// ─── QNT-229 #2a: synthesize composing indicator ──────────────────────────
//
// Fills the dead-air window between the last tool returning and the analyst
// voice arriving (the synthesize + narrate-startup gap — post-QNT-220 mean
// ~4.5s). Sits in the SAME slot as the narrative bubble (top, above the card),
// so when narration starts it is simply replaced by NarrativeBubble in place —
// the early card (QNT-229 #2b) renders BELOW and never gets shoved by a bubble
// appearing above it. A 4x4 pixel-spinner precedes the intent-named label
// ("composing thesis…").

import type { Intent } from "@/lib/api";

import { composingLabel } from "../chat-run";
import { PixelSpinner } from "./pixel-spinner";

export function ComposingBubble({ intent }: { intent: Intent | null }) {
  return (
    <div
      aria-label="Composing answer"
      aria-busy="true"
      className="flex items-center gap-2 rounded border border-zinc-800 bg-zinc-900/40 px-3 py-2"
    >
      <PixelSpinner />
      <span className="font-mono text-[11px] uppercase tracking-wider text-zinc-400">
        {composingLabel(intent)}
      </span>
    </div>
  );
}
