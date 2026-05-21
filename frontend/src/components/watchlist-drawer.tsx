"use client";

import { useState, useEffect } from "react";
import { ChartDataIcon } from "@/components/icons/chart-data";

type Props = {
  watchlist: React.ReactNode;
};

/**
 * Tablet/small-desktop watchlist drawer (768–1279px).
 * Renders a header bar (md:flex xl:hidden) with a watchlist toggle button,
 * plus a slide-in panel and backdrop. At ≥1280px the watchlist lives in the
 * grid rail; at <768px MobileNav handles watchlist access.
 */
export function WatchlistDrawer({ watchlist }: Props) {
  const [open, setOpen] = useState(false);

  // Close drawer when viewport enters the xl range (1280px+) where the
  // watchlist moves into the persistent grid rail — prevents stale open
  // state if the user resizes back down.
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1280px)");
    const handler = (e: MediaQueryListEvent) => { if (e.matches) setOpen(false); };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  return (
    <>
      {/* Header bar — visible at 768–1279px */}
      <header className="hidden md:flex xl:hidden h-10 shrink-0 items-center gap-3 border-b border-zinc-800 bg-zinc-950 px-3">
        <button
          onClick={() => setOpen((v) => !v)}
          aria-label={open ? "Close watchlist" : "Open watchlist"}
          className="flex h-7 w-7 items-center justify-center rounded text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-100"
        >
          {open ? (
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="h-4 w-4" aria-hidden="true">
              <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" />
            </svg>
          ) : (
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="h-4 w-4" aria-hidden="true">
              <path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z" />
            </svg>
          )}
        </button>
        <ChartDataIcon className="h-5 w-5 flex-shrink-0 text-emerald-400" />
        <span className="font-mono text-xs font-semibold uppercase tracking-widest text-zinc-400">
          Equity Data Agent
        </span>
      </header>

      {/* Slide-in panel — positioned below the header bar (top-10).
          [&_aside>div:first-child]:hidden suppresses the TERMINAL branding
          since the header bar above already carries the identity. */}
      <div
        className={`hidden md:block xl:hidden absolute top-10 bottom-0 left-0 z-40 w-[17rem]
          transform transition-transform duration-300 ease-in-out overflow-y-auto
          [&_aside>div:first-child]:hidden
          ${open ? "translate-x-0" : "-translate-x-full"}`}
        onClick={(e) => {
          if ((e.target as Element).closest("a")) setOpen(false);
        }}
      >
        {watchlist}
      </div>

      {/* Backdrop */}
      {open && (
        <div
          className="hidden md:block xl:hidden absolute top-10 inset-x-0 bottom-0 z-30 bg-black/50"
          onClick={() => setOpen(false)}
          aria-hidden="true"
        />
      )}
    </>
  );
}
