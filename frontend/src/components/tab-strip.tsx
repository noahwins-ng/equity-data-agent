"use client";

// ─── Shared compact tab strip (QNT-253) ───────────────────────────────────
//
// Extracted from the byte-identical tab markup that the technicals and
// fundamentals cards duplicated: a compact pill row where each tab shows a
// short glyph below the `wide:` breakpoint and the full label above it. Driven
// by a `{ id, label, short }` list so any card with that tab shape can reuse it
// (QNT-251 tab-role a11y lives here once, not per consumer).

type TabItem<T extends string> = { id: T; label: string; short: string };

export function TabStrip<T extends string>({
  tabs,
  active,
  onSelect,
  ariaLabel,
}: {
  tabs: readonly TabItem<T>[];
  active: T;
  onSelect: (id: T) => void;
  ariaLabel: string;
}) {
  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className="flex gap-0.5 text-[10px] wide:gap-1"
    >
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={active === t.id}
          aria-label={t.label}
          onClick={() => onSelect(t.id)}
          className={
            active === t.id
              ? "rounded border border-zinc-600 bg-zinc-800 px-1.5 py-0.5 uppercase text-zinc-100"
              : "rounded border border-transparent px-1.5 py-0.5 uppercase text-zinc-400 hover:bg-zinc-900"
          }
        >
          <span className="wide:hidden">{t.short}</span>
          <span className="hidden wide:inline">{t.label}</span>
        </button>
      ))}
    </div>
  );
}
