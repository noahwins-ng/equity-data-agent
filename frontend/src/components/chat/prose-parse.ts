// ─── Narrate-bubble prose parser (QNT-285) ────────────────────────────────
//
// The synthesis prompts produce inline citations like `(source: technical)`,
// rendered as muted chips. `parseInlineChips` is the legacy chip-only tokenizer
// every card uses (single paragraph, no markdown) — preserved byte-for-byte so
// the BLUF change stays scoped to the narrate bubble.
//
// `parseProse` is the richer layer the narrate bubble opts into: the narrate
// node now speaks in a BLUF shape (a **bold** call, a blank line, synthesis
// prose, an optional "Watch:" line). It splits paragraph blocks and parses
// bold, on top of the same chip transform. Parsing is pure so the structure can
// be unit-tested with node:test without rendering React — mirrors the
// price-chart-legend.ts split.

export type ProseSegment =
  | { type: "text"; text: string }
  | { type: "bold"; text: string }
  | { type: "chip"; text: string };

// A line is a flat list of segments; a block (paragraph) is a list of lines
// separated by single newlines; the document is blocks separated by blank lines.
export type ProseLine = ProseSegment[];
export type ProseBlock = ProseLine[];

// The chip class matches letters, the multi-source separators (`|` and `,`),
// and whitespace — so `(source: technical, fundamental)` chips as one token
// instead of silently falling through to plain text.
const CHIP_ONLY_PATTERN = /\(source:\s*([A-Za-z|,\s]+)\)/g;

// **bold** OR (source: name). Bold stops at a newline so a malformed unbalanced
// `**` cannot greedily swallow across paragraphs.
const TOKEN_PATTERN = /\*\*([^*\n]+)\*\*|\(source:\s*([A-Za-z|,\s]+)\)/g;

// ─── QNT-287: consecutive same-source de-duplication ──────────────────────
//
// Within one answer the synthesis tags nearly every clause with the same
// source, so a dim chip repeated 3-6x still reads as noise. Collapse it: a chip
// is emitted only when its source differs from the last EMITTED source; an
// immediate repeat is dropped (a transition — news -> fundamental -> news —
// still shows every chip). `DedupeState` carries the last-emitted source: a
// per-call default de-dups within one text; a shared carrier (threaded by
// AspectBlock across its summary + bullet ProseBlocks) de-dups a single-source
// aspect down to one chip. Normalisation makes the match case/whitespace-
// insensitive so `News` and `news ` are the same source.
export type DedupeState = { last: string | null };

function normaliseSource(raw: string): string {
  return raw.toLowerCase().replace(/\s+/g, " ").trim();
}

// Emit a chip unless it repeats the last-emitted source. On a drop, trim a
// single trailing space off the preceding text so the dropped `(source: x)`
// doesn't leave an orphan space before the following punctuation/word
// (`oversight (source: news).` -> `oversight.`, not `oversight .`).
function pushChip(segments: ProseSegment[], rawSource: string, st: DedupeState): void {
  const text = rawSource.trim();
  const key = normaliseSource(text);
  if (key === st.last) {
    const prev = segments[segments.length - 1];
    if (prev && prev.type === "text") prev.text = prev.text.replace(/ $/, "");
    return;
  }
  segments.push({ type: "chip", text });
  st.last = key;
}

// Legacy chip-only tokenizer (single paragraph, no bold). Used by every
// non-narrate caller so their output is unchanged by QNT-285.
export function parseInlineChips(text: string, dedupe?: DedupeState): ProseSegment[] {
  if (!text) return [];
  const st = dedupe ?? { last: null };
  const segments: ProseSegment[] = [];
  let lastIdx = 0;
  for (const match of text.matchAll(CHIP_ONLY_PATTERN)) {
    const start = match.index ?? 0;
    if (start > lastIdx) {
      segments.push({ type: "text", text: text.slice(lastIdx, start) });
    }
    pushChip(segments, match[1], st);
    lastIdx = start + match[0].length;
  }
  if (lastIdx < text.length) {
    segments.push({ type: "text", text: text.slice(lastIdx) });
  }
  return segments;
}

function tokenizeLine(line: string, st: DedupeState): ProseSegment[] {
  const segments: ProseSegment[] = [];
  let lastIdx = 0;
  for (const match of line.matchAll(TOKEN_PATTERN)) {
    const start = match.index ?? 0;
    if (start > lastIdx) {
      segments.push({ type: "text", text: line.slice(lastIdx, start) });
    }
    if (match[1] !== undefined) {
      segments.push({ type: "bold", text: match[1].trim() });
    } else if (match[2] !== undefined) {
      pushChip(segments, match[2], st);
    }
    lastIdx = start + match[0].length;
  }
  if (lastIdx < line.length) {
    segments.push({ type: "text", text: line.slice(lastIdx) });
  }
  return segments;
}

export function parseProse(text: string, dedupe?: DedupeState): ProseBlock[] {
  if (!text.trim()) return [];
  // One carrier for the whole document so a repeated source collapses across
  // paragraphs/lines, not just within a single line (the synthesis bubble is
  // one parseProse call spanning every paragraph).
  const st = dedupe ?? { last: null };
  return text
    .split(/\n\s*\n/)
    .map((block) =>
      block
        .split("\n")
        .map((line) => tokenizeLine(line, st))
        .filter((line) => line.length > 0),
    )
    .filter((block) => block.length > 0);
}
