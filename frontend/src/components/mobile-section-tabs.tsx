"use client";

import { useEffect, useState } from "react";

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

function useIsXlViewport() {
  const [isXl, setIsXl] = useState<boolean | null>(null);

  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1280px)");
    const sync = () => setIsXl(mq.matches);
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);

  return isXl;
}

/**
 * On mobile (<768px): renders a tab bar and shows only the selected section.
 * On tablet/desktop (≥768px): renders all three sections in the auto-fit grid.
 */
export function MobileSectionTabs({ technicals, fundamentals, news }: Props) {
  const [active, setActive] = useState<Section>("technicals");
  const isXl = useIsXlViewport();

  const content: Record<Section, React.ReactNode> = { technicals, fundamentals, news };

  if (isXl === null) {
    return <div className="flex min-h-0 flex-1 flex-col" />;
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {isXl ? (
        <div className="grid min-h-0 flex-1 grid-cols-3 gap-3 overflow-hidden px-6 py-2">
          {technicals}
          {fundamentals}
          {news}
        </div>
      ) : (
        <>
          <div role="tablist" aria-label="Section" className="flex shrink-0 border-b border-zinc-800">
            {SECTIONS.map(({ key, label }) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={active === key}
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

          <div className="flex min-h-0 flex-1 flex-col overflow-hidden [&>*]:min-h-0 [&>*]:flex-1">
            {content[active]}
          </div>
        </>
      )}
    </div>
  );
}
