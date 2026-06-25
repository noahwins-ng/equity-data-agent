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

import { type ProseSegment, parseInlineChips, parseProse } from "./prose-parse";

function renderSegment(seg: ProseSegment, key: number) {
  if (seg.type === "chip") {
    return (
      <span
        key={key}
        className="mx-0.5 inline-block rounded border border-zinc-700 bg-zinc-900 px-1 py-px font-mono text-[10px] uppercase tracking-wide text-zinc-400"
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

export function ProseBlock({ text, rich = false }: { text: string; rich?: boolean }) {
  if (!text.trim()) return null;

  if (!rich) {
    const segments = parseInlineChips(text);
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
