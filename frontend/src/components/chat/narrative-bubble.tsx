// ─── QNT-211: streaming narrative bubble ──────────────────────────────────
//
// Renders the narrate-node output ABOVE the structured card. Plain prose,
// left-aligned, neutral surface — matches the rhythm of an analyst speaking
// while the card composes beneath. The card is unchanged; the bubble is
// purely additive.

import type { AnchorSource } from "./prose-parse";
import { ProseBlock } from "./prose-block";

export function NarrativeBubble({
  text,
  sources,
}: {
  text: string;
  sources?: readonly AnchorSource[];
}) {
  if (!text.trim()) return null;
  return (
    <div className="rounded border border-zinc-800 bg-zinc-900/40 px-3 py-2">
      {/* QNT-305: the narrate voice is the shape most prone to a bad `[Rn]` /
        `(source: … Rn)`; pass the retrieved rows so the parser de-anchors any id
        that is out of range or points at the wrong corpus (the backend strip
        does not reach this streamed bubble). */}
      <ProseBlock text={text} rich sources={sources} />
    </div>
  );
}
