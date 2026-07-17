// ─── Suggestion button (QNT-178) ──────────────────────────────────────────
//
// Shared by the cold-start ``EmptyState`` (prefills composer) and the mid-
// conversation ``ConversationalCard`` (auto-sends). Same visual; different
// click contracts — the button itself is dumb, click behaviour is parent-
// driven.

export function SuggestionButton({
  text,
  onClick,
  disabled = false,
}: {
  text: string;
  onClick: () => void;
  // QNT-379: auto-send chips are disabled while another run streams — the
  // composer already is, and an un-guarded chip click mid-stream was the
  // trigger for the aborted-run composer deadlock.
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="w-full rounded border border-zinc-800 bg-zinc-950/60 px-2 py-1 text-left font-mono text-[11px] text-zinc-300 transition hover:enabled:border-zinc-600 hover:enabled:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40"
    >
      {text}
    </button>
  );
}
