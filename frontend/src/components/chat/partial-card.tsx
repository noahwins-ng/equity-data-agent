// ─── QNT-494: card in progress (card_partial) ─────────────────────────────
//
// Renders the completed fields of a card synthesize is still generating, so
// the panel fills in during the ~4-8s structured call instead of sitting
// blank. One generic renderer for every streamed shape: the real card swaps in
// when the final validated event lands (run-reducer clears the partial).
//
// Never renders a verdict or label pill -- those come only from the final
// validated card. Any field may be absent; render only what has arrived.

import type { AspectView, CardPartialEvent, RetrievedSource } from "@/lib/api";

import { AspectBlock } from "./aspect-block";
import { ProseBlock } from "./prose-block";

const SLOT_TITLE: Record<CardPartialEvent["slot"], string> = {
  thesis: "Thesis",
  quick_fact: "Quick fact",
  comparison: "Comparison",
  focused: "Focused read",
  exploration: "Exploration",
};

// Pill / discriminator fields: the verdict family belongs to the final card,
// and the rest are not prose.
const HIDDEN_KEYS = new Set(["verdict", "label", "focus", "source", "ticker"]);

// The final cards hide this one prose field while the run streams (QNT-229:
// narrate's bubble is the prose surface, so card prose must not show and then
// retract). The partial matches, and thesis verdict_rationale also stays out
// because it names the verdict before validation.
const DEMOTED_PROSE: Partial<Record<CardPartialEvent["slot"], string>> = {
  thesis: "verdict_rationale",
  focused: "summary",
  exploration: "headline",
  comparison: "differences",
};

function title(key: string): string {
  return key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

// An aspect block (thesis / comparison section) once its summary is complete,
// with its label pill withheld until the final card.
function asAspect(value: Record<string, unknown>): AspectView | null {
  if (typeof value.summary !== "string") return null;
  return {
    label: null,
    summary: value.summary,
    supports: strings(value.supports),
    challenges: strings(value.challenges),
  };
}

function Field({
  name,
  value,
  sources,
}: {
  name: string;
  value: unknown;
  sources: RetrievedSource[];
}) {
  if (HIDDEN_KEYS.has(name) || value === null || value === undefined) return null;
  if (typeof value === "string") {
    return (
      <div>
        <div className="mb-0.5 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
          {title(name)}
        </div>
        <ProseBlock text={value} sources={sources} />
      </div>
    );
  }
  if (isRecord(value)) {
    const aspect = asAspect(value);
    return aspect ? <AspectBlock title={title(name)} aspect={aspect} /> : null;
  }
  if (Array.isArray(value)) {
    const items = value.map((item, i) => {
      if (typeof item === "string") return <ProseBlock key={i} text={item} sources={sources} />;
      if (!isRecord(item)) return null;
      // Cited-value chip ({label, value}) or a comparison section.
      if (typeof item.label === "string" && typeof item.value === "string") {
        return (
          <p key={i} className="font-mono text-[11px] text-zinc-300">
            {item.label}: {item.value}
          </p>
        );
      }
      return (
        <div key={i} className="space-y-2">
          {typeof item.ticker === "string" && (
            <div className="font-mono text-[10px] uppercase tracking-wider text-zinc-400">
              {item.ticker}
            </div>
          )}
          {Object.entries(item).map(([k, v]) => (
            <Field key={k} name={k} value={v} sources={sources} />
          ))}
        </div>
      );
    });
    if (items.every((item) => item === null)) return null;
    return (
      <div>
        <div className="mb-0.5 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
          {title(name)}
        </div>
        <div className="space-y-1">{items}</div>
      </div>
    );
  }
  return null;
}

export function PartialCard({
  ticker,
  partial,
  sources,
}: {
  ticker: string | null;
  partial: CardPartialEvent;
  sources: RetrievedSource[];
}) {
  return (
    <section aria-busy="true" className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        {SLOT_TITLE[partial.slot]} · {ticker ?? "session"} · writing…
      </header>
      <div className="space-y-3 p-3">
        {Object.entries(partial.card)
          .filter(([k]) => k !== DEMOTED_PROSE[partial.slot])
          .map(([k, v]) => (
            <Field key={k} name={k} value={v} sources={sources} />
          ))}
      </div>
    </section>
  );
}
