// ─── QNT-211: streaming narrative bubble ──────────────────────────────────
//
// Renders the narrate-node output ABOVE the structured card. Plain prose,
// left-aligned, neutral surface — matches the rhythm of an analyst speaking
// while the card composes beneath. The card is unchanged; the bubble is
// purely additive.

import { ProseBlock } from "./prose-block";

export function NarrativeBubble({ text, maxAnchor }: { text: string; maxAnchor?: number }) {
  if (!text.trim()) return null;
  return (
    <div className="rounded border border-zinc-800 bg-zinc-900/40 px-3 py-2">
      {/* QNT-305: the narrate voice is the shape most prone to appending a
        fabricated `[Rn]`; pass the retrieved-row count so the parser de-anchors
        any id past it (the backend strip does not reach this streamed bubble). */}
      <ProseBlock text={text} rich maxAnchor={maxAnchor} />
    </div>
  );
}
