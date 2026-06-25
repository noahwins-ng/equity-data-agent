// ─── QNT-211: streaming narrative bubble ──────────────────────────────────
//
// Renders the narrate-node output ABOVE the structured card. Plain prose,
// left-aligned, neutral surface — matches the rhythm of an analyst speaking
// while the card composes beneath. The card is unchanged; the bubble is
// purely additive.

import { ProseBlock } from "./prose-block";

export function NarrativeBubble({ text }: { text: string }) {
  if (!text.trim()) return null;
  return (
    <div className="rounded border border-zinc-800 bg-zinc-900/40 px-3 py-2">
      <ProseBlock text={text} rich />
    </div>
  );
}
