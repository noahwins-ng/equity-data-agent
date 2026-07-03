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

// QNT-301: `anchor` carries a retrieved-source id (`R1`, `R2`, ...) when the
// citation was of the form `(source: news R1)`. Present only on anchored chips;
// canned citations (`(source: technical)`) leave it undefined and render exactly
// as before.
export type ProseSegment =
  | { type: "text"; text: string }
  | { type: "bold"; text: string }
  | { type: "chip"; text: string; anchor?: string };

// A line is a flat list of segments; a block (paragraph) is a list of lines
// separated by single newlines; the document is blocks separated by blank lines.
export type ProseLine = ProseSegment[];
export type ProseBlock = ProseLine[];

// The chip class matches letters, the multi-source separators (`|` and `,`),
// and whitespace — so `(source: technical, fundamental)` chips as one token
// instead of silently falling through to plain text. QNT-301: an optional
// trailing `R\d+` id captures the retrieved-source anchor in
// `(source: news R1)` as its own group (the source class is lazy so the id
// splits off cleanly). The id group is undefined for canned citations.
//
// QNT-301: a SECOND alternative `\[(R\d+)\]` catches a BARE `[R1]` tag. The
// tag prefixes every folded retrieved bullet; the thesis/quick_fact prompts
// convert it to the `(source: … R1)` form, but the narrate voice (news /
// followup answers, which cite as `(publisher, date)`) tends to append the raw
// `[R1]` instead. Recognising the bare form here is the render-boundary
// guarantee that a raw `[R1]` never reaches the user — it becomes the same
// anchored chip, so the leak turns into the anchor.
const CHIP_ONLY_PATTERN = /\(source:\s*([A-Za-z|,\s]+?)(?:\s+(R\d+))?\)|\[(R\d+)\]/g;

// **bold** OR (source: name [Rn]) OR a bare [Rn] tag. Bold stops at a newline
// so a malformed unbalanced `**` cannot greedily swallow across paragraphs.
const TOKEN_PATTERN =
  /\*\*([^*\n]+)\*\*|\(source:\s*([A-Za-z|,\s]+?)(?:\s+(R\d+))?\)|\[(R\d+)\]/g;

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
function pushChip(
  segments: ProseSegment[],
  rawSource: string,
  st: DedupeState,
  anchor?: string,
): void {
  const text = rawSource.trim();
  // QNT-301: fold the anchor into the dedupe key so two claims citing distinct
  // retrieved rows (R1 vs R2) both render — collapsing them would erase the
  // per-claim provenance the anchoring exists to show. A canned repeat still
  // de-dups (no anchor → key is just the source).
  const key = anchor ? `${normaliseSource(text)} ${anchor}` : normaliseSource(text);
  if (key === st.last) {
    const prev = segments[segments.length - 1];
    if (prev && prev.type === "text") prev.text = prev.text.replace(/ $/, "");
    return;
  }
  segments.push(anchor ? { type: "chip", text, anchor } : { type: "chip", text });
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
    // match[3] = a bare `[Rn]` tag (no source name); otherwise a `(source: …)`
    // citation with match[1]=source, match[2]=optional anchor.
    if (match[3] !== undefined) {
      pushChip(segments, "", st, match[3]);
    } else {
      pushChip(segments, match[1], st, match[2]);
    }
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
    } else if (match[4] !== undefined) {
      // Bare `[Rn]` tag — anchor chip with no source label.
      pushChip(segments, "", st, match[4]);
    } else if (match[2] !== undefined) {
      pushChip(segments, match[2], st, match[3]);
    }
    lastIdx = start + match[0].length;
  }
  if (lastIdx < line.length) {
    segments.push({ type: "text", text: line.slice(lastIdx) });
  }
  return segments;
}

// QNT-303 follow-up: the narrate model is inconsistent about the separator
// before its "Watch:" close -- a blank line, a single newline, or just a space
// (confirmed across prod traces). Left to the model it renders glued to the
// synthesis with no spacing and no styling. Normalise it here to a paragraph
// break plus a bold label so the close always renders as its own spaced,
// emphasised block -- a deterministic render-boundary guarantee (cf. the
// QNT-301 bare-`[Rn]` handling) rather than trusting model formatting. The
// literal "Watch:" (capital W, as the prompt emits and every sample shows) is
// matched once so only the close is promoted, never a "watch:" mid-prose.
function normaliseWatchClose(text: string): string {
  return text.replace(/\s*\bWatch:/, "\n\n**Watch:**");
}

export function parseProse(text: string, dedupe?: DedupeState): ProseBlock[] {
  if (!text.trim()) return [];
  // One carrier for the whole document so a repeated source collapses across
  // paragraphs/lines, not just within a single line (the synthesis bubble is
  // one parseProse call spanning every paragraph).
  const st = dedupe ?? { last: null };
  return normaliseWatchClose(text)
    .split(/\n\s*\n/)
    .map((block) =>
      block
        .split("\n")
        .map((line) => tokenizeLine(line, st))
        .filter((line) => line.length > 0),
    )
    .filter((block) => block.length > 0);
}
