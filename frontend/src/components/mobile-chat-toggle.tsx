"use client";

import { useState } from "react";
import { ChatPanel } from "@/components/chat-panel";
import { ChartDataIcon } from "@/components/icons/chart-data";

type Panel = "chat" | "watchlist" | null;

type Props = {
  watchlist: React.ReactNode;
};

/**
 * Mobile/tablet nav header with hamburger menu.
 * Renders a header bar (lg:hidden) + panel overlays for Watchlist and Chat.
 * Placed above the main grid in the root layout; panel overlays sit below the bar.
 */
export function MobileNav({ watchlist }: Props) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [panel, setPanel] = useState<Panel>(null);

  function openPanel(p: Panel) {
    setPanel(p);
    setMenuOpen(false);
  }

  function closeAll() {
    setPanel(null);
    setMenuOpen(false);
  }

  return (
    <>
      {/* Header bar — only visible on mobile (<768px) */}
      <header className="flex h-10 shrink-0 items-center gap-3 border-b border-zinc-800 bg-zinc-950 px-3 md:hidden">
        <button
          onClick={() => (panel !== null ? closeAll() : setMenuOpen((v) => !v))}
          className="flex h-7 w-7 items-center justify-center rounded text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-100"
          aria-label={panel !== null ? "Close panel" : menuOpen ? "Close menu" : "Menu"}
        >
          {panel !== null || menuOpen ? (
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="h-4 w-4" aria-hidden="true">
              <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" />
            </svg>
          ) : (
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="h-4 w-4" aria-hidden="true">
              <path d="M3 18h18v-2H3v2zm0-5h18v-2H3v2zm0-7v2h18V6H3z" />
            </svg>
          )}
        </button>
        <ChartDataIcon className="h-5 w-5 flex-shrink-0 text-emerald-400" />
        <span className="font-mono text-xs font-semibold uppercase tracking-widest text-zinc-400">
          Equity Data Agent
        </span>
      </header>

      {/* Dropdown menu — appears below header bar */}
      {menuOpen && (
        <div className="absolute left-0 right-0 top-10 z-50 border-b border-zinc-800 bg-zinc-900 md:hidden">
          <button
            onClick={() => openPanel("watchlist")}
            className="flex w-full items-center gap-2 px-4 py-3 font-mono text-xs uppercase tracking-wider text-zinc-300 transition hover:bg-zinc-800 hover:text-zinc-100"
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="h-3.5 w-3.5" aria-hidden="true">
              <path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z" />
            </svg>
            Watchlist
          </button>
          <div className="mx-4 border-t border-zinc-800 md:hidden" />
          <button
            onClick={() => openPanel("chat")}
            className="flex w-full items-center gap-2 px-4 py-3 font-mono text-xs uppercase tracking-wider text-zinc-300 transition hover:bg-zinc-800 hover:text-zinc-100 md:hidden"
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="h-3.5 w-3.5" aria-hidden="true">
              <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z" />
            </svg>
            Chat
          </button>
        </div>
      )}

      {/* Panel overlays — fill remaining height below header */}
      {panel === "watchlist" && (
        <div
          className="absolute inset-x-0 bottom-0 top-10 z-40 overflow-y-auto md:hidden [&_aside>div:first-child]:hidden"
          onClick={(e) => {
            if ((e.target as Element).closest("a")) closeAll();
          }}
        >
          {watchlist}
        </div>
      )}
      {panel === "chat" && (
        <div className="absolute inset-x-0 bottom-0 top-10 z-40 md:hidden">
          <ChatPanel />
        </div>
      )}
    </>
  );
}
