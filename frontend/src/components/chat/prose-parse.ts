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

// Legacy chip-only tokenizer (single paragraph, no bold). Used by every
// non-narrate caller so their output is unchanged by QNT-285.
export function parseInlineChips(text: string): ProseSegment[] {
  if (!text) return [];
  const segments: ProseSegment[] = [];
  let lastIdx = 0;
  for (const match of text.matchAll(CHIP_ONLY_PATTERN)) {
    const start = match.index ?? 0;
    if (start > lastIdx) {
      segments.push({ type: "text", text: text.slice(lastIdx, start) });
    }
    segments.push({ type: "chip", text: match[1].trim() });
    lastIdx = start + match[0].length;
  }
  if (lastIdx < text.length) {
    segments.push({ type: "text", text: text.slice(lastIdx) });
  }
  return segments;
}

function tokenizeLine(line: string): ProseSegment[] {
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
      segments.push({ type: "chip", text: match[2].trim() });
    }
    lastIdx = start + match[0].length;
  }
  if (lastIdx < line.length) {
    segments.push({ type: "text", text: line.slice(lastIdx) });
  }
  return segments;
}

export function parseProse(text: string): ProseBlock[] {
  if (!text.trim()) return [];
  return text
    .split(/\n\s*\n/)
    .map((block) =>
      block
        .split("\n")
        .map((line) => tokenizeLine(line))
        .filter((line) => line.length > 0),
    )
    .filter((block) => block.length > 0);
}
