// ─── Suggestion button (QNT-178) ──────────────────────────────────────────
//
// Shared by the cold-start ``EmptyState`` (prefills composer) and the mid-
// conversation ``ConversationalCard`` (auto-sends). Same visual; different
// click contracts — the button itself is dumb, click behaviour is parent-
// driven.

export function SuggestionButton({
  text,
  onClick,
}: {
  text: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full rounded border border-zinc-800 bg-zinc-950/60 px-2 py-1 text-left font-mono text-[11px] text-zinc-300 transition hover:border-zinc-600 hover:text-zinc-100"
    >
      {text}
    </button>
  );
}
