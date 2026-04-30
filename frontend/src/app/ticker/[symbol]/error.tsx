"use client";

/**
 * Per-route error boundary for `/ticker/[symbol]`.
 *
 * The route fetches multiple FastAPI surfaces in parallel; each is wrapped in
 * a try/catch so a single endpoint outage degrades to its empty state rather
 * than throwing. This boundary catches anything that does throw past the
 * page handlers — typically a Next.js render-time misconfiguration rather
 * than data-layer failure.
 */

import { useEffect } from "react";

export default function TickerError({
  error,
  reset,
}: {
  error: Error;
  reset: () => void;
}) {
  useEffect(() => {
    // Surface the message in the dev console so the cause is recoverable
    // without round-tripping through Vercel logs.
    console.error("ticker page error", error);
  }, [error]);

  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
      <h1 className="text-xl font-semibold text-zinc-100">Couldn&apos;t load ticker</h1>
      <p className="max-w-md text-sm text-zinc-400">{error.message}</p>
      <button
        type="button"
        onClick={reset}
        className="rounded border border-zinc-700 px-3 py-1 text-xs uppercase tracking-wider text-zinc-300 hover:bg-zinc-900"
      >
        Retry
      </button>
    </div>
  );
}
