/**
 * Landing route — middle pane empty state (ADR-014 §2).
 *
 * Static at build time: no per-request data on the page itself; the
 * watchlist (in the layout above) provides the only data on this view.
 */
export const dynamic = "force-static";

export default function Home() {
  return (
    <div className="flex h-full items-center justify-center p-8 text-center">
      <div className="max-w-md space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">
          Select a ticker
        </h1>
        <p className="text-sm text-zinc-400">
          Choose one from the watchlist on the left to view price action,
          indicators, fundamentals, and news.
        </p>
      </div>
    </div>
  );
}
