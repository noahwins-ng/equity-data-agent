"use client";

import { useEffect, useState } from "react";

import { ChatPanel } from "@/components/chat-panel";
import { ChartDataIcon } from "@/components/icons/chart-data";

type MobilePanel = "chat" | "watchlist" | null;

type Props = {
  children: React.ReactNode;
  watchlist: React.ReactNode;
};

export function AppShell({ children, watchlist }: Props) {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [mobilePanel, setMobilePanel] = useState<MobilePanel>(null);
  const [tabletWatchlistOpen, setTabletWatchlistOpen] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1280px)");
    const handler = (event: MediaQueryListEvent) => {
      if (event.matches) {
        setTabletWatchlistOpen(false);
        setMobilePanel(null);
        setMobileMenuOpen(false);
      }
    };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  function openMobilePanel(panel: MobilePanel) {
    setMobilePanel(panel);
    setMobileMenuOpen(false);
  }

  function closeMobile() {
    setMobilePanel(null);
    setMobileMenuOpen(false);
  }

  return (
    <>
      <header className="flex h-10 shrink-0 items-center gap-3 border-b border-zinc-800 bg-zinc-950 px-3 md:hidden">
        <button
          onClick={() =>
            mobilePanel !== null ? closeMobile() : setMobileMenuOpen((value) => !value)
          }
          className="flex h-7 w-7 items-center justify-center rounded text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-100"
          aria-label={
            mobilePanel !== null ? "Close panel" : mobileMenuOpen ? "Close menu" : "Menu"
          }
        >
          {mobilePanel !== null || mobileMenuOpen ? (
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="currentColor"
              className="h-4 w-4"
              aria-hidden="true"
            >
              <path d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" />
            </svg>
          ) : (
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="currentColor"
              className="h-4 w-4"
              aria-hidden="true"
            >
              <path d="M3 18h18v-2H3v2zm0-5h18v-2H3v2zm0-7v2h18V6H3z" />
            </svg>
          )}
        </button>
        <ChartDataIcon className="h-5 w-5 flex-shrink-0 text-emerald-400" />
        <span className="font-mono text-xs font-semibold uppercase tracking-widest text-zinc-400">
          Equity Data Agent
        </span>
      </header>

      {mobileMenuOpen && (
        <div className="absolute left-0 right-0 top-10 z-50 border-b border-zinc-800 bg-zinc-900 md:hidden">
          <button
            onClick={() => openMobilePanel("watchlist")}
            className="flex w-full items-center gap-2 px-4 py-3 font-mono text-xs uppercase tracking-wider text-zinc-300 transition hover:bg-zinc-800 hover:text-zinc-100"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="currentColor"
              className="h-3.5 w-3.5"
              aria-hidden="true"
            >
              <path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z" />
            </svg>
            Watchlist
          </button>
          <div className="mx-4 border-t border-zinc-800 md:hidden" />
          <button
            onClick={() => openMobilePanel("chat")}
            className="flex w-full items-center gap-2 px-4 py-3 font-mono text-xs uppercase tracking-wider text-zinc-300 transition hover:bg-zinc-800 hover:text-zinc-100 md:hidden"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="currentColor"
              className="h-3.5 w-3.5"
              aria-hidden="true"
            >
              <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z" />
            </svg>
            Chat
          </button>
        </div>
      )}

      <header className="hidden h-10 shrink-0 items-center gap-3 border-b border-zinc-800 bg-zinc-950 px-3 md:flex xl:hidden">
        <button
          onClick={() => setTabletWatchlistOpen((value) => !value)}
          aria-label={tabletWatchlistOpen ? "Close watchlist" : "Open watchlist"}
          className="flex h-7 w-7 items-center justify-center rounded text-zinc-400 transition hover:bg-zinc-800 hover:text-zinc-100"
        >
          {tabletWatchlistOpen ? (
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="currentColor"
              className="h-4 w-4"
              aria-hidden="true"
            >
              <path d="M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" />
            </svg>
          ) : (
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="currentColor"
              className="h-4 w-4"
              aria-hidden="true"
            >
              <path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z" />
            </svg>
          )}
        </button>
        <ChartDataIcon className="h-5 w-5 flex-shrink-0 text-emerald-400" />
        <span className="font-mono text-xs font-semibold uppercase tracking-widest text-zinc-400">
          Equity Data Agent
        </span>
      </header>

      <div className="relative grid min-h-0 flex-1 grid-cols-1 overflow-hidden md:grid-cols-[minmax(0,1fr)_clamp(18rem,30%,22rem)] lg:grid-cols-[minmax(0,1fr)_clamp(22rem,28%,26rem)] xl:grid-cols-[17rem_minmax(0,1fr)_clamp(22rem,26%,28rem)]">
        <div
          className={`absolute inset-x-0 bottom-0 top-0 z-40 overflow-y-auto md:inset-x-auto md:w-[17rem] md:transform md:transition-transform md:duration-300 md:ease-in-out xl:static xl:z-auto xl:block xl:w-auto xl:translate-x-0 xl:transform-none xl:overflow-hidden
            [&_aside>div:first-child]:hidden xl:[&_aside>div:first-child]:flex
            ${
              mobilePanel === "watchlist"
                ? "block md:hidden"
                : tabletWatchlistOpen
                  ? "hidden md:block"
                  : "hidden md:block md:-translate-x-full"
            }`}
          onClick={(event) => {
            if ((event.target as Element).closest("a")) {
              closeMobile();
              setTabletWatchlistOpen(false);
            }
          }}
        >
          {watchlist}
        </div>

        {tabletWatchlistOpen && (
          <div
            className="absolute inset-x-0 bottom-0 top-0 z-30 hidden bg-black/50 md:block xl:hidden"
            onClick={() => setTabletWatchlistOpen(false)}
            aria-hidden="true"
          />
        )}

        <main
          id="main"
          tabIndex={-1}
          className="min-h-0 overflow-y-auto outline-none xl:col-start-2"
        >
          {children}
        </main>

        {/* QNT-256: ONE ChatPanel instance, positioned by CSS (mirrors the
            watchlist node above). On <md it is an absolute overlay gated
            visible by `mobilePanel === "chat"`; on md+ it is the static grid
            rail. Never conditionally mounted — so closing the mobile overlay
            only hides it and the runs/thread_id state survives reopen. */}
        <div
          className={`absolute inset-x-0 bottom-0 top-0 z-40 min-h-0 flex-col md:static md:inset-auto md:z-auto md:flex xl:col-start-3 ${
            mobilePanel === "chat" ? "flex" : "hidden md:flex"
          }`}
        >
          <ChatPanel />
        </div>
      </div>
    </>
  );
}
