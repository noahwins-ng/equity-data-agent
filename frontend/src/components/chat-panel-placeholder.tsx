"use client";

/**
 * Right-rail chat panel placeholder.
 *
 * Marked "use client" at scaffold time so QNT-74 can wire SSE consumption
 * (fetch + ReadableStream + eventsource-parser) without re-classifying the
 * boundary. Per ADR-014 §4 and Anti-pattern #6: chat is a persistent panel
 * inside app/layout.tsx, NOT a /chat route — a route would tear down the
 * SSE stream on every ticker navigation.
 */
export function ChatPanelPlaceholder() {
  return (
    <aside
      aria-label="Agent chat"
      className="flex h-full flex-col gap-2 border-l border-zinc-800 bg-zinc-950 p-4 text-zinc-400"
    >
      <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
        Agent
      </h2>
      <p className="text-sm">Pending QNT-74.</p>
    </aside>
  );
}
