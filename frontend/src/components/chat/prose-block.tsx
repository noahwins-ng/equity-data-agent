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

// QNT-301: an anchored citation (`(source: news R1)`) scrolls to the matching
// Retrieved-sources row and flashes a ring on it. The row lives in the same run
// `<article>` as the chip, so we resolve it by walking up to that ancestor and
// querying its `data-source-id` — scoping the lookup to this run avoids id
// collisions across the multi-run tape without threading a run id through every
// card.
function scrollToSource(anchor: string, target: HTMLElement): void {
  const row = target.closest("article")?.querySelector<HTMLElement>(
    `[data-source-id="${anchor}"]`,
  );
  if (!row) return;
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.classList.add("ring-1", "ring-emerald-400/70");
  window.setTimeout(() => row.classList.remove("ring-1", "ring-emerald-400/70"), 1600);
}

function renderSegment(seg: ProseSegment, key: number) {
  if (seg.type === "chip" && seg.anchor) {
    // Anchored retrieved-source citation: a clickable link-chip. Kept in the
    // same quiet family as the canned chip but tinted emerald + underlined so it
    // reads as interactive, and it names the row id so the jump target is
    // legible before the click.
    const anchor = seg.anchor;
    return (
      <button
        key={key}
        type="button"
        onClick={(e) => scrollToSource(anchor, e.currentTarget)}
        className="mx-0.5 inline-flex items-center gap-0.5 rounded border border-emerald-700/50 bg-emerald-950/30 px-1 py-px font-mono text-[10px] uppercase tracking-wide text-emerald-300 underline decoration-dotted underline-offset-2 transition hover:bg-emerald-900/40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-emerald-400/70"
        aria-label={`Jump to retrieved source ${anchor}`}
      >
        {seg.text ? `${seg.text} ${anchor}` : anchor}
      </button>
    );
  }
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
  maxAnchor,
}: {
  text: string;
  rich?: boolean;
  dedupe?: DedupeState;
  // QNT-305: the count of retrieved-sources rows this run. When set, the parser
  // de-anchors any retrieved id above it (an out-of-range, fabricated anchor).
  // Threaded from RunBlock into the streamed narrate/prose surfaces, which the
  // backend strip cannot reach (they stream as deltas, not card payloads).
  maxAnchor?: number;
}) {
  if (!text.trim()) return null;

  if (!rich) {
    const segments = parseInlineChips(text, dedupe, maxAnchor);
    return (
      <p className="text-xs leading-relaxed text-zinc-200">
        {segments.map((seg, i) => renderSegment(seg, i))}
      </p>
    );
  }

  const blocks = parseProse(text, undefined, maxAnchor);
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
