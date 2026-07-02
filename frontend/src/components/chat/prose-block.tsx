// ─── Inline-chip prose renderer ───────────────────────────────────────────
//
// The synthesis prompt produces inline citations like `(source: technical)`
// and free-text values inside the prose. The chat-panel design wants
// "value · source · date" chips. We surface the citation as a chip rendered
// in monospaced muted style.
//
// QNT-285: the narrate bubble opts into a BLUF shape via `rich` — a **bold**
// call, a blank line, synthesis prose, and an optional "Watch:" line. Without
// `rich` (every other card) the output is unchanged: one <p>, chips only, no
// bold, newlines collapsed — keeping the change scoped to the narrate bubble
// and avoiding a block element inside the inline contexts those callers use.

import { type DedupeState, type ProseSegment, parseInlineChips, parseProse } from "./prose-parse";

function renderSegment(seg: ProseSegment, key: number) {
  if (seg.type === "chip") {
    // QNT-295: a subtle boxed pill, restored. QNT-286 went borderless to kill
    // the "ransom-note" repetition, but with the box gone the label read as
    // unstyled noise. QNT-287 fixed the actual cause (repetition — now one chip
    // per source-run), so the box is viable again and looks intentional. Kept
    // deliberately quieter than the tone-coloured classification badges
    // (VERDICT / ASPECT_LABEL): a muted zinc border (700/60) + faint fill so a
    // source citation reads as secondary to a classification label. Tone holds
    // at zinc-400 / 10px for WCAG AA.
    return (
      <span
        key={key}
        className="mx-0.5 inline-block rounded border border-zinc-700/60 bg-zinc-900/50 px-1 py-px font-mono text-[10px] uppercase tracking-wide text-zinc-400"
        title="cited source"
      >
        {seg.text}
      </span>
    );
  }
  if (seg.type === "bold") {
    return (
      <strong key={key} className="font-semibold text-zinc-100">
        {seg.text}
      </strong>
    );
  }
  return <span key={key}>{seg.text}</span>;
}

// QNT-287: `dedupe` is an optional shared carrier. AspectBlock passes one
// across its summary + bullet ProseBlocks so a single-source aspect de-dups to
// one chip; every other caller omits it and de-dups within its own text. Only
// the non-rich path threads it — the rich (narrate) bubble is a single
// ProseBlock whose parseProse already de-dups across its own paragraphs.
export function ProseBlock({
  text,
  rich = false,
  dedupe,
}: {
  text: string;
  rich?: boolean;
  dedupe?: DedupeState;
}) {
  if (!text.trim()) return null;

  if (!rich) {
    const segments = parseInlineChips(text, dedupe);
    return (
      <p className="text-xs leading-relaxed text-zinc-200">
        {segments.map((seg, i) => renderSegment(seg, i))}
      </p>
    );
  }

  const blocks = parseProse(text);
  if (blocks.length === 0) return null;
  return (
    <div className="space-y-2">
      {blocks.map((lines, bi) => (
        <p key={bi} className="text-xs leading-relaxed text-zinc-200">
          {lines.map((segments, li) => (
            <span key={li}>
              {li > 0 && <br />}
              {segments.map((seg, si) => renderSegment(seg, si))}
            </span>
          ))}
        </p>
      ))}
    </div>
  );
}
