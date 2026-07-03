// Run with: `npm test` (from `frontend/`) — Node's built-in TS loader + node:test.
//
// QNT-285: the narrate bubble renders a BLUF shape (bold call + synthesis prose
// + optional Watch line). These pin the pure parser that ProseBlock renders
// from: bold extraction, paragraph/line splitting, and that the existing
// `(source:)` → chip transform still fires.

import test from "node:test";
import assert from "node:assert/strict";

import {
  type DedupeState,
  type ProseSegment,
  parseInlineChips,
  parseProse,
} from "./prose-parse.ts";

const chipTexts = (segs: { type: string; text: string }[]) =>
  segs.filter((s) => s.type === "chip").map((s) => s.text);

// QNT-301: chip segments narrowed so the optional `anchor` id is accessible.
const chips = (segs: ProseSegment[]) =>
  segs.filter((s): s is Extract<ProseSegment, { type: "chip" }> => s.type === "chip");

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
  const blocks = parseProse("Driver one.\nSecond driver.");
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].length, 2);
  assert.equal(blocks[0][1][0].text, "Second driver.");
});

// QNT-303 follow-up: the "Watch:" close renders as its own spaced block with a
// bold label, regardless of the separator the model emitted before it.
for (const [name, sep] of [
  ["single newline", "\n"],
  ["blank line", "\n\n"],
  ["just a space", " "],
] as const) {
  test(`Watch close is promoted to its own bold block (separator: ${name})`, () => {
    const blocks = parseProse(`The trend is intact.${sep}Watch: the next print.`);
    assert.equal(blocks.length, 2, "synthesis and Watch must be separate blocks");
    // First block is the synthesis, second is the Watch close.
    assert.equal(blocks[0][0][0].text, "The trend is intact.");
    const watchSeg = blocks[1][0][0];
    assert.equal(watchSeg.type, "bold");
    assert.equal(watchSeg.text, "Watch:");
    // The catalyst text follows the bold label as plain text.
    assert.equal(blocks[1][0][1].text, " the next print.");
  });
}

test("a lowercase 'watch:' mid-prose is not promoted (only the capital-W close)", () => {
  const blocks = parseProse("Keep a close watch: on margins next quarter.");
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0][0][0].type, "text");
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
  // QNT-303 follow-up: three blocks -- the bold call, the synthesis, and the
  // promoted bold Watch close (previously the Watch line was glued into the
  // synthesis block as a second <br> line).
  assert.equal(blocks.length, 3);
  assert.equal(blocks[0][0][0].type, "bold");
  // synthesis block: one line, two chips.
  assert.equal(blocks[1].length, 1);
  const chipCount = blocks[1][0].filter((s) => s.type === "chip").length;
  assert.equal(chipCount, 2);
  // Watch block: bold label leads.
  assert.equal(blocks[2][0][0].type, "bold");
  assert.equal(blocks[2][0][0].text, "Watch:");
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

// ─── QNT-301: claim-anchored retrieved-source citations ───────────────────

test("an anchored citation carries its retrieved-source id; a canned one does not", () => {
  const cs = chips(
    parseInlineChips("Buyback expanded (source: news R1). RSI firm (source: technical)."),
  );
  assert.equal(cs[0].text, "news");
  assert.equal(cs[0].anchor, "R1");
  assert.equal(cs[1].text, "technical");
  assert.equal(cs[1].anchor, undefined, "a canned citation stays anchor-less");
});

test("the anchor splits cleanly off the source name (source text has no id)", () => {
  // The id must not leak into the chip label — `(source: fundamental R3)` chips
  // as text "fundamental" + anchor "R3", so the pill reads "fundamental R3".
  const [chip] = chips(parseInlineChips("Guidance raised (source: fundamental R3)."));
  assert.equal(chip.text, "fundamental");
  assert.equal(chip.anchor, "R3");
});

test("distinct retrieved anchors both render — they are not de-duped", () => {
  // Two claims citing different rows (R1 vs R2) must each keep their chip: the
  // per-claim provenance is the whole point of anchoring.
  const cs = chips(parseInlineChips("A (source: news R1). B (source: news R2)."));
  assert.deepEqual(
    cs.map((c) => c.anchor),
    ["R1", "R2"],
  );
});

test("bold + anchored chip coexist in the narrate BLUF path (parseProse)", () => {
  const blocks = parseProse(
    "**Constructive.**\n\nGuidance was raised (source: fundamental R2).",
  );
  const [chip] = chips(blocks[1][0]);
  assert.equal(chip.text, "fundamental");
  assert.equal(chip.anchor, "R2");
});

test("a bare [Rn] tag becomes an anchor chip — a raw tag never renders as text", () => {
  // The narrate voice (news answers) tends to append the raw [R1] tag next to a
  // (publisher, date) citation instead of the (source: news R1) form. The parser
  // must turn that bare tag into the same anchored chip so a literal "[R1]" can
  // never reach the user.
  const segs = parseInlineChips("Rubin platform (finnhub, 2026-06-27) [R1] is material.");
  const cs = chips(segs);
  assert.equal(cs.length, 1);
  assert.equal(cs[0].text, ""); // no source label on a bare tag
  assert.equal(cs[0].anchor, "R1");
  // No text segment retains the raw bracket tag.
  const allText = segs
    .filter((s) => s.type === "text")
    .map((s) => s.text)
    .join("");
  assert.ok(!allText.includes("[R1]"), "the raw [R1] must not survive as text");
});

test("a bare [Rn] tag is recognised in the narrate BLUF path too (parseProse)", () => {
  const blocks = parseProse("**Constructive.**\n\nThe deal (finnhub, 2026-06-30) [R3] expands reach.");
  const cs = chips(blocks[1][0]);
  assert.equal(cs[0].anchor, "R3");
  assert.equal(cs[0].text, "");
});

// ─── QNT-305: de-anchor an untrustworthy retrieved id ─────────────────────
// The synthesis model fabricates ids past the number of rows retrieved (only
// R1/R2 retrieved, answer cites R5/R11) OR mis-staples an in-range id onto the
// wrong corpus (`fundamental R1` where R1 is a news row). Either way the id
// points at the wrong (or no) `data-source-id` row, so it must not render as a
// clickable anchor. `sources` is the retrieved-rows list; passing rows without a
// `corpus` tag exercises the range-only check (QNT-305 original behaviour).

// n untagged rows -> range check only (no corpus to compare against).
const bare = (n: number) => Array.from({ length: n }, () => ({}));

test("an out-of-range (source: name Rn) drops the id but keeps the source chip", () => {
  // Only 2 rows retrieved; R5 is fabricated -> render as the canned `news` chip.
  const cs = chips(parseInlineChips("Buyback expanded (source: news R5).", undefined, bare(2)));
  assert.equal(cs.length, 1);
  assert.equal(cs[0].text, "news");
  assert.equal(cs[0].anchor, undefined, "the fabricated id must not anchor");
});

test("an in-range anchor still renders as an anchored chip (control)", () => {
  const cs = chips(parseInlineChips("Buyback expanded (source: news R2).", undefined, bare(2)));
  assert.equal(cs[0].text, "news");
  assert.equal(cs[0].anchor, "R2");
});

test("an out-of-range bare [Rn] tag is dropped entirely, leaving no chip or text", () => {
  const segs = parseInlineChips(
    "Rubin platform (finnhub, 2026-06-27) [R11] is material.",
    undefined,
    bare(2),
  );
  assert.deepEqual(chips(segs), [], "the fabricated bare tag is dropped");
  const allText = segs
    .filter((s) => s.type === "text")
    .map((s) => s.text)
    .join("");
  assert.ok(!allText.includes("[R11]"), "the raw tag must not survive as text");
  assert.ok(!allText.includes("  "), "no orphan double space is left behind");
});

test("with zero retrieved rows every anchor is de-anchored", () => {
  const cs = chips(parseInlineChips("A claim (source: news R1).", undefined, bare(0)));
  assert.equal(cs[0].anchor, undefined);
  assert.equal(cs[0].text, "news");
});

test("sources undefined leaves anchors untouched (existing callers)", () => {
  const cs = chips(parseInlineChips("A claim (source: news R9)."));
  assert.equal(cs[0].anchor, "R9");
});

test("the narrate BLUF path de-anchors an out-of-range tag too (parseProse)", () => {
  const blocks = parseProse(
    "**Constructive.**\n\nThe deal (finnhub, 2026-06-30) [R11] expands reach (source: news R5).",
    undefined,
    bare(2),
  );
  const cs = chips(blocks[1][0]);
  // The bare [R11] is dropped; the (source: news R5) keeps its source, loses R5.
  assert.equal(cs.length, 1);
  assert.equal(cs[0].text, "news");
  assert.equal(cs[0].anchor, undefined);
});

// ─── QNT-305 follow-up: corpus-consistency (in-range but wrong corpus) ─────

test("a corpus-mismatched (source: fundamental R1) on a news row drops the id", () => {
  // R1 is a NEWS row; `fundamental R1` mis-staples a news id onto a fundamental
  // claim. In range (1 of 1) so the range check passes -- the corpus check must
  // catch it: keep the plain `fundamental` chip, drop the R1 anchor.
  const cs = chips(
    parseInlineChips("Growth strong (source: fundamental R1).", undefined, [{ corpus: "news" }]),
  );
  assert.equal(cs[0].text, "fundamental");
  assert.equal(cs[0].anchor, undefined, "a wrong-corpus id must not anchor");
});

test("a corpus-matched (source: news R1) on a news row still anchors", () => {
  const cs = chips(
    parseInlineChips("Deal closed (source: news R1).", undefined, [{ corpus: "news" }]),
  );
  assert.equal(cs[0].text, "news");
  assert.equal(cs[0].anchor, "R1");
});

test("(source: fundamental R1) on an earnings row anchors (news folds elsewhere)", () => {
  const cs = chips(
    parseInlineChips("Guidance raised (source: fundamental R1).", undefined, [
      { corpus: "earnings" },
    ]),
  );
  assert.equal(cs[0].text, "fundamental");
  assert.equal(cs[0].anchor, "R1");
});

test("a never-retrieval-backed name (technical Rn) is always de-anchored", () => {
  const cs = chips(
    parseInlineChips("RSI firm (source: technical R1).", undefined, [{ corpus: "news" }]),
  );
  assert.equal(cs[0].text, "technical");
  assert.equal(cs[0].anchor, undefined);
});

test("a bare [Rn] is corpus-agnostic -- kept when in range on any corpus", () => {
  const segs = parseInlineChips("The deal (finnhub) [R1] matters.", undefined, [{ corpus: "news" }]);
  const cs = chips(segs);
  assert.equal(cs[0].anchor, "R1");
});
