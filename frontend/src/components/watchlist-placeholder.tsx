/**
 * Left-rail watchlist placeholder.
 *
 * QNT-72 fills this in with /api/v1/dashboard/summary data. This stub holds
 * the layout slot so the three-pane geometry is complete from QNT-71 onward.
 */
export function WatchlistPlaceholder() {
  return (
    <aside
      aria-label="Watchlist"
      className="flex h-full flex-col gap-2 border-r border-zinc-800 bg-zinc-950 p-4 text-zinc-400"
    >
      <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
        Watchlist
      </h2>
      <p className="text-sm">Pending QNT-72.</p>
    </aside>
  );
}
