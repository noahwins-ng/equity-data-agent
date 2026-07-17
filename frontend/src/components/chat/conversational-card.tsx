// ─── Conversational redirect card (QNT-156) ───────────────────────────────
//
// Used both for greetings/off-domain asks AND as the deterministic fallback
// when any other intent fails to produce a payload. Renders the prose answer
// + an optional bulleted suggestion list. Click a suggestion to drop it into
// the composer (parent-driven via ``onSuggestion``).

import type { ConversationalPayload } from "@/lib/api";

import { ProseBlock } from "./prose-block";
import { SuggestionButton } from "./suggestion-button";

export function ConversationalCard({
  conversational,
  onSuggestion,
  suggestionsDisabled,
}: {
  conversational: ConversationalPayload;
  onSuggestion: (q: string) => void;
  // QNT-379: chips auto-send, so they are inert while another run streams.
  suggestionsDisabled: boolean;
}) {
  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="flex items-baseline justify-between gap-2 border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span>Analyst · session</span>
      </header>

      <div className="space-y-3 p-3">
        <ProseBlock text={conversational.answer} />
        {conversational.suggestions.length > 0 && (
          <div>
            <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              You could ask
            </div>
            <ul className="space-y-1">
              {conversational.suggestions.map((s, i) => (
                <li key={i}>
                  {/* Mid-conversation redirect: clicking auto-sends because
                      the user has already committed to asking. EmptyState
                      uses the same SuggestionButton but its parent prefills
                      the composer instead — different surface, different
                      contract. */}
                  <SuggestionButton
                    text={s}
                    onClick={() => onSuggestion(s)}
                    disabled={suggestionsDisabled}
                  />
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  );
}
