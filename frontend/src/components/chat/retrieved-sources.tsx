// ─── Retrieved sources (QNT-226) ──────────────────────────────────────────
//
// Provenance for the agent's semantic news search: the articles RAG actually
// surfaced this turn, shown as a compact clickable list under the analyst
// voice. On a targeted news ask the focused-news card is dropped (the voice
// answers it), so this list is the structured surface that shows the user
// WHICH headlines grounded the answer. Mirrors the external-link idiom in
// ticker/news-card.tsx (new tab, noopener).

import type { RetrievedSource } from "@/lib/api";

export function RetrievedSources({ sources }: { sources: RetrievedSource[] }) {
  if (sources.length === 0) return null;
  return (
    <section
      aria-label="Retrieved sources"
      className="rounded border border-zinc-800 bg-zinc-950/40 p-2"
    >
      <h3 className="px-1 pb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
        Retrieved sources · {sources.length}
      </h3>
      <ul className="space-y-0.5">
        {sources.map((src, i) => (
          <li
            // QNT-301: `data-source-id` is the jump target an anchored inline
            // citation (`(source: news R1)`) scrolls to; `scroll-mt-2` keeps the
            // row off the top edge when centred, and `rounded` lets the flash
            // ring wrap the whole row.
            key={`${src.url || src.headline}-${i}`}
            data-source-id={src.id}
            tabIndex={-1}
            className="scroll-mt-2 rounded transition"
          >
            {src.url ? (
              <a
                href={src.url}
                target="_blank"
                rel="noopener noreferrer"
                className="group flex items-baseline gap-1.5 rounded px-1 py-1 transition hover:bg-zinc-900 focus-visible:bg-zinc-900 focus-visible:outline-none"
              >
                {src.id && (
                  <span className="mt-px shrink-0 font-mono text-[10px] uppercase tracking-wider text-emerald-400/80">
                    {src.id}
                  </span>
                )}
                <span className="flex min-w-0 flex-col">
                  <span className="text-xs text-zinc-200 group-hover:text-emerald-300">
                    {src.headline}
                  </span>
                  <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">
                    {[src.source, src.date].filter(Boolean).join(" · ")}
                  </span>
                </span>
              </a>
            ) : (
              <div className="flex items-baseline gap-1.5 px-1 py-1">
                {src.id && (
                  <span className="mt-px shrink-0 font-mono text-[10px] uppercase tracking-wider text-emerald-400/80">
                    {src.id}
                  </span>
                )}
                <span className="flex min-w-0 flex-col">
                  <span className="text-xs text-zinc-200">{src.headline}</span>
                  <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">
                    {[src.source, src.date].filter(Boolean).join(" · ")}
                  </span>
                </span>
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
