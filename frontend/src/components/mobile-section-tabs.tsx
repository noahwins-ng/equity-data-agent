"use client";

import { useState } from "react";

type Section = "technicals" | "fundamentals" | "news";

type Props = {
  technicals: React.ReactNode;
  fundamentals: React.ReactNode;
  news: React.ReactNode;
};

const SECTIONS: { key: Section; label: string }[] = [
  { key: "technicals", label: "Technicals" },
  { key: "fundamentals", label: "Fundamentals" },
  { key: "news", label: "News" },
];

/**
 * On mobile (<768px): renders a tab bar and shows only the selected section.
 * On tablet/desktop (≥768px): renders all three sections in the auto-fit grid.
 */
export function MobileSectionTabs({ technicals, fundamentals, news }: Props) {
  const [active, setActive] = useState<Section>("technicals");

  const content: Record<Section, React.ReactNode> = { technicals, fundamentals, news };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Tab bar — mobile + tablet (< 1280px) */}
      <div className="flex shrink-0 border-b border-zinc-800 xl:hidden">
        {SECTIONS.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => setActive(key)}
            className={`flex-1 py-2 font-mono text-xs uppercase tracking-wider transition
              ${
                active === key
                  ? "border-b-2 border-emerald-400 text-emerald-400"
                  : "text-zinc-500 hover:text-zinc-300"
              }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Mobile + tablet: only active section — [&>*] ensures the card fills the
          container so its internal overflow-y-auto scroll works */}
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden xl:hidden [&>*]:min-h-0 [&>*]:flex-1">
        {content[active]}
      </div>

      {/* ≥1280px: 3-column grid, cards fill container height with inner scroll. */}
      <div className="hidden min-h-0 flex-1 gap-3 overflow-hidden px-6 py-2 xl:grid xl:grid-cols-3">
        {technicals}
        {fundamentals}
        {news}
      </div>
    </div>
  );
}
