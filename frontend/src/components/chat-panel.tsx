"use client";

/**
 * Right-rail agent chat panel (QNT-74).
 *
 * Per ADR-014 §4 + Anti-pattern #6: chat is a persistent panel inside
 * `app/layout.tsx`, NEVER a route — a `/chat` route would tear down the
 * SSE stream on every ticker navigation. The active ticker is read from
 * `usePathname()` so the panel observes the URL but never owns it.
 *
 * Per ADR-008: no Vercel AI SDK. The transport is `fetch` returning a
 * `ReadableStream`, parsed by `lib/sse.ts` (~50 lines, hand-rolled).
 *
 * Composer placeholder source list comes from `/api/v1/health` provenance
 * (QNT-132) — vendor swap on the backend re-renders the placeholder
 * without a frontend deploy.
 */

import { usePathname } from "next/navigation";
import {
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  API_BASE_URL,
  type AspectLabel,
  type AspectView,
  type ChatErrorEvent,
  type ComparisonPayload,
  type ConversationalPayload,
  type DoneEvent,
  type ExplorationAnswerPayload,
  type FocusedAnalysisPayload,
  type FocusKind,
  type HealthResponse,
  type Intent,
  type IntentEvent,
  type LeanComparisonPayload,
  type NarrativeChunkEvent,
  type ProseChunkEvent,
  type QuickFactPayload,
  type RetrievedSource,
  type RetrievedSourcesEvent,
  type ThesisPayload,
  type ToolCallEvent,
  type ToolResultEvent,
  type Verdict,
} from "@/lib/api";
import { composingLabel, hasAnswerSurface, isComposing, showCardProse } from "./chat-run";
import { parseSseStream } from "@/lib/sse";

// ─── Local message-tape model ─────────────────────────────────────────────
//
// One run = one user-prompted exchange. The tape stores at most one in-flight
// run plus the prior runs in this session (no cross-session persistence —
// out of scope per QNT-130). Each run carries the tool rows it observed,
// the streamed prose deltas, the final thesis (if any), and terminal stats.

type RunStatus = "streaming" | "done" | "errored";

type ToolRow = ToolCallEvent & {
  result?: ToolResultEvent;
};

type ChatRun = {
  id: string;
  ticker: string | null;
  prompt: string;
  startedAt: number;
  status: RunStatus;
  intent: Intent | null;
  toolRows: ToolRow[];
  proseChunks: string[];
  // QNT-211: accumulated narrative_chunk deltas — rendered as a prose
  // bubble ABOVE the structured card. Empty string means "no narrative
  // yet" (narrate hasn't started or failed silently); the bubble only
  // renders when non-empty.
  narrative: string;
  thesis: ThesisPayload | null;
  quickFact: QuickFactPayload | null;
  comparison: ComparisonPayload | null;
  comparisonLean: LeanComparisonPayload | null;
  conversational: ConversationalPayload | null;
  focused: FocusedAnalysisPayload | null;
  exploration: ExplorationAnswerPayload | null;
  // QNT-226: articles the semantic news search surfaced this turn. Rendered
  // as a compact clickable provenance list. Empty when no search ran.
  retrievedSources: RetrievedSource[];
  errors: ChatErrorEvent[];
  stats: DoneEvent | null;
};

const MAX_RUNS = 20; // soft cap: one session, prevent unbounded growth

// Active ticker is encoded in /ticker/[symbol] — the panel observes the
// pathname rather than holding it in state. ADR-014 §4 anti-pattern note:
// rename the route segment? update this regex too.
const TICKER_PATH = /^\/ticker\/([^/]+)/i;

function activeTickerFromPath(path: string | null): string | null {
  if (!path) return null;
  const m = path.match(TICKER_PATH);
  return m ? m[1].toUpperCase() : null;
}

function redactUnsupportedNumbers(text: string, unsupported: readonly string[] = []): string {
  let cleaned = text;
  for (const raw of unsupported.filter(Boolean).sort((a, b) => b.length - a.length)) {
    const escaped = raw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const pattern = new RegExp(`(^|[^\\d.])(${escaped})(?=$|[^\\d.])`, "g");
    cleaned = cleaned.replace(pattern, "$1[unsupported number]");
  }
  return cleaned;
}

// ─── Inline data chip parser ──────────────────────────────────────────────
//
// The synthesis prompt produces inline citations like `(source: technical)`
// and free-text values inside the prose. The chat-panel design wants
// "value · source · date" chips. We surface the citation as a chip rendered
// in monospaced muted style; the prose author chooses how dense to be.
// Falls back gracefully when no chip-shaped tokens are present.

const CHIP_PATTERN = /\(source:\s*([A-Za-z|\s]+)\)/g;

type ProseSegment = { type: "text"; text: string } | { type: "chip"; text: string };

function splitProseIntoSegments(text: string): ProseSegment[] {
  if (!text) return [];
  const segments: ProseSegment[] = [];
  let lastIdx = 0;
  for (const match of text.matchAll(CHIP_PATTERN)) {
    const matchStart = match.index ?? 0;
    if (matchStart > lastIdx) {
      segments.push({ type: "text", text: text.slice(lastIdx, matchStart) });
    }
    segments.push({ type: "chip", text: match[1].trim() });
    lastIdx = matchStart + match[0].length;
  }
  if (lastIdx < text.length) {
    segments.push({ type: "text", text: text.slice(lastIdx) });
  }
  return segments;
}

// ─── Suggestion button (QNT-178) ──────────────────────────────────────────
//
// Shared by the cold-start ``EmptyState`` (prefills composer) and the mid-
// conversation ``ConversationalCard`` (auto-sends). Same visual; different
// click contracts — the button itself is dumb, click behaviour is parent-
// driven.

function SuggestionButton({
  text,
  onClick,
}: {
  text: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full rounded border border-zinc-800 bg-zinc-950/60 px-2 py-1 text-left font-mono text-[11px] text-zinc-300 transition hover:border-zinc-600 hover:text-zinc-100"
    >
      {text}
    </button>
  );
}

// ─── Composer — input + send ───────────────────────────────────────────────
//
// QNT-176: tools/cite toggles removed. Tools were never optional from the
// user's perspective (every useful chat answer needs them; ``cite_sources``
// was a no-op on the backend). Hiding those behind toggles taught the wrong
// mental model.
//
// QNT-178: now a controlled component. The parent owns ``value`` so an
// ``EmptyState`` suggestion click can prefill the textarea. ``focusKey`` is
// a counter the parent bumps to request a focus — Composer watches it via
// useEffect and calls ``textareaRef.current.focus()``. Avoids forwardRef
// since the parent never needs to call focus() directly; it just bumps the
// key after setting the value.

function Composer({
  ticker,
  sources,
  disabled,
  value,
  onChange,
  onSubmit,
  focusKey,
}: {
  ticker: string | null;
  sources: string[];
  disabled: boolean;
  value: string;
  onChange: (next: string) => void;
  onSubmit: (input: string) => void;
  focusKey: number;
}) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const placeholder = useMemo(() => {
    const focus = ticker ? ticker : "the watchlist";
    if (sources.length === 0) {
      return `Ask the analyst about ${focus}...`;
    }
    return `Ask the analyst about ${focus}... (cites ${sources.join(", ")})`;
  }, [ticker, sources]);

  // Focus the textarea whenever the parent bumps focusKey. Skip the first
  // render (focusKey starts at 0) so opening the page doesn't steal focus
  // from the rest of the layout.
  useEffect(() => {
    if (focusKey === 0) return;
    textareaRef.current?.focus();
  }, [focusKey]);

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSubmit(trimmed);
    onChange("");
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-2 border-t border-zinc-800 bg-zinc-950 p-3"
    >
      <div className="flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wider">
        {ticker ? (
          <span className="rounded border border-emerald-700/40 bg-emerald-900/20 px-1.5 py-0.5 font-mono text-emerald-400">
            @{ticker}
          </span>
        ) : (
          <span className="rounded border border-zinc-800 bg-zinc-900/60 px-1.5 py-0.5 font-mono text-zinc-500">
            no ticker
          </span>
        )}
      </div>
      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          rows={2}
          placeholder={placeholder}
          disabled={disabled}
          className="min-h-[2.5rem] flex-1 resize-none rounded border border-zinc-800 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-100 placeholder:text-zinc-600 focus:border-zinc-600 focus:outline-none disabled:opacity-60"
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              handleSubmit(e as unknown as FormEvent);
            }
          }}
        />
        <button
          type="submit"
          disabled={disabled || !value.trim()}
          className="h-8 rounded bg-emerald-600 px-3 text-[10px] font-semibold uppercase tracking-wider text-white transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Send
        </button>
      </div>
    </form>
  );
}

// ─── Empty state — cold-start suggestions (QNT-178) ───────────────────────
//
// Replaces the placeholder line with 4 suggestion buttons covering the
// agent's main answer shapes (thesis + the 3 QNT-176 focused intents).
// Click prefills the composer + focuses; the user can edit before pressing
// Send. No-ticker landing state is unchanged ("Pick a ticker from the
// watchlist first") because the composer is disabled until they pick one.

function emptyStateSuggestions(ticker: string): string[] {
  return [
    `Give me a balanced thesis on ${ticker}`,
    `Technical analysis of ${ticker}`,
    `Walk me through ${ticker}'s fundamentals`,
    `What's the news on ${ticker}?`,
  ];
}

function EmptyState({
  ticker,
  onSuggestion,
}: {
  ticker: string | null;
  onSuggestion: (q: string) => void;
}) {
  if (!ticker) {
    return (
      <div className="flex h-full items-center justify-center px-6 text-center">
        <p className="text-xs text-zinc-500">
          Pick a ticker from the watchlist to start a research session.
        </p>
      </div>
    );
  }
  const suggestions = emptyStateSuggestions(ticker);
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-6">
      <p className="text-center text-sm text-zinc-300">
        What would you like to know about {ticker}?
      </p>
      <ul className="w-full max-w-sm space-y-1">
        {suggestions.map((s) => (
          <li key={s}>
            <SuggestionButton text={s} onClick={() => onSuggestion(s)} />
          </li>
        ))}
      </ul>
    </div>
  );
}

// ─── Tool-call row ────────────────────────────────────────────────────────

function ToolCallRow({ row }: { row: ToolRow }) {
  const args = Object.entries(row.args)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(" · ");
  const result = row.result;
  const status = result ? (result.ok ? "✓" : "✗") : "…";
  const statusColor = result
    ? result.ok
      ? "text-emerald-400"
      : "text-red-400"
    : "text-zinc-500";

  return (
    <div className="flex items-baseline justify-between gap-2 font-mono text-[11px] text-zinc-400">
      <div className="min-w-0 flex-1 truncate">
        <span className="text-zinc-500">→ </span>
        <span className="text-zinc-200">{row.label}</span>
        {args && <span className="text-zinc-500"> · {args}</span>}
        {result && <span className="text-zinc-500"> · {result.summary}</span>}
      </div>
      <div className="flex shrink-0 items-baseline gap-1 tabular-nums">
        {result && (
          <span className="text-zinc-500">{result.latency_ms}ms</span>
        )}
        <span className={statusColor}>{status}</span>
      </div>
    </div>
  );
}

// ─── Inline-chip prose renderer ───────────────────────────────────────────

function ProseBlock({ text }: { text: string }) {
  if (!text.trim()) return null;
  const segments = splitProseIntoSegments(text);
  return (
    <p className="text-xs leading-relaxed text-zinc-200">
      {segments.map((seg, i) =>
        seg.type === "chip" ? (
          <span
            key={i}
            className="mx-0.5 inline-block rounded border border-zinc-700 bg-zinc-900 px-1 py-px font-mono text-[10px] uppercase tracking-wide text-zinc-400"
            title="cited source"
          >
            {seg.text}
          </span>
        ) : (
          <span key={i}>{seg.text}</span>
        ),
      )}
    </p>
  );
}

// ─── QNT-211: streaming narrative bubble ──────────────────────────────────
//
// Renders the narrate-node output ABOVE the structured card. Plain prose,
// left-aligned, neutral surface — matches the rhythm of an analyst speaking
// while the card composes beneath. The card is unchanged; the bubble is
// purely additive.

function NarrativeBubble({ text }: { text: string }) {
  if (!text.trim()) return null;
  return (
    <div className="rounded border border-zinc-800 bg-zinc-900/40 px-3 py-2">
      <ProseBlock text={text} />
    </div>
  );
}

// ─── QNT-229 #2a: synthesize composing indicator ──────────────────────────
//
// Fills the dead-air window between the last tool returning and the analyst
// voice arriving (the synthesize + narrate-startup gap — post-QNT-220 mean
// ~4.5s). Sits in the SAME slot as the narrative bubble (top, above the card),
// so when narration starts it is simply replaced by NarrativeBubble in place —
// the early card (QNT-229 #2b) renders BELOW and never gets shoved by a bubble
// appearing above it. A 4x4 pixel-spinner precedes the intent-named label
// ("composing thesis…").

// NxN pixel grid: a lit pixel chases clockwise around the perimeter (a fine
// pixel-ring loader). The exact centre pixel breathes as a nucleus; the rest of
// the interior is an empty spacer so the ring reads cleanly. PIXEL_SPIN_MS MUST
// equal the .pixel-chase CSS animation-duration so the negative-delay stagger
// spans exactly one loop (otherwise the comet has a seam).
const PIXEL_GRID = 3;
const PIXEL_SPIN_MS = 1000;

// Perimeter cell indices (row-major) clockwise from the top-left corner.
function ringPerimeter(n: number): number[] {
  const cells: number[] = [];
  for (let c = 0; c < n; c++) cells.push(c); // top row, L→R
  for (let r = 1; r < n; r++) cells.push(r * n + (n - 1)); // right col, T→B
  for (let c = n - 2; c >= 0; c--) cells.push((n - 1) * n + c); // bottom row, R→L
  for (let r = n - 2; r >= 1; r--) cells.push(r * n); // left col, B→T
  return cells;
}
const PIXEL_PERIMETER = ringPerimeter(PIXEL_GRID);
const PIXEL_CENTER = Math.floor(PIXEL_GRID / 2) * PIXEL_GRID + Math.floor(PIXEL_GRID / 2);

function PixelSpinner() {
  const order = new Array<number>(PIXEL_GRID * PIXEL_GRID).fill(-1);
  PIXEL_PERIMETER.forEach((cellIdx, i) => {
    order[cellIdx] = i;
  });
  return (
    <span aria-hidden className="grid shrink-0 grid-cols-3 gap-0.5">
      {order.map((ord, idx) => {
        if (ord !== -1) {
          return (
            <span
              key={idx}
              className="pixel-chase h-1.5 w-1.5 rounded-[1px] bg-emerald-400"
              style={{
                // Negative stagger keyed to (len - ord) so the bright head
                // travels FORWARD along the perimeter (clockwise).
                animationDelay: `-${((PIXEL_PERIMETER.length - ord) / PIXEL_PERIMETER.length) * PIXEL_SPIN_MS}ms`,
              }}
            />
          );
        }
        if (idx === PIXEL_CENTER) {
          // Single breathing nucleus at the centre of the ring.
          return <span key={idx} className="pixel-core h-1.5 w-1.5 rounded-[1px] bg-emerald-500" />;
        }
        return <span key={idx} className="h-1.5 w-1.5" />; // interior spacer (none at 3x3)
      })}
    </span>
  );
}

function ComposingBubble({ intent }: { intent: Intent | null }) {
  return (
    <div
      aria-label="Composing answer"
      aria-busy="true"
      className="flex items-center gap-2 rounded border border-zinc-800 bg-zinc-900/40 px-3 py-2"
    >
      <PixelSpinner />
      <span className="font-mono text-[11px] uppercase tracking-wider text-zinc-400">
        {composingLabel(intent)}
      </span>
    </div>
  );
}

// ─── QNT-208: four-aspect thesis card ─────────────────────────────────────

// Verdict pill palette. Overweight = emerald (constructive), Neutral = zinc
// (balanced), Underweight = red (negative). Pydantic bounds the verdict on
// the server side; an exhaustive map means a future verdict value lights up
// a type error rather than a missing className at runtime.
const VERDICT_PILL: Record<Verdict, string> = {
  Overweight: "border-emerald-700/40 bg-emerald-900/20 text-emerald-300",
  Neutral: "border-zinc-700 bg-zinc-900/40 text-zinc-300",
  Underweight: "border-red-700/40 bg-red-900/20 text-red-300",
};

// Per-aspect label chip palette. Premium / Uptrend = green; Discounted /
// Downtrend = red; Inline / Sideways = zinc; null label = no chip rendered.
const ASPECT_LABEL_PILL: Record<AspectLabel, string> = {
  Premium: "border-amber-700/40 bg-amber-900/20 text-amber-300",
  Inline: "border-zinc-700 bg-zinc-900/40 text-zinc-300",
  Discounted: "border-emerald-700/40 bg-emerald-900/20 text-emerald-300",
  Uptrend: "border-emerald-700/40 bg-emerald-900/20 text-emerald-300",
  Sideways: "border-zinc-700 bg-zinc-900/40 text-zinc-300",
  Downtrend: "border-red-700/40 bg-red-900/20 text-red-300",
};

// QNT-213: the thesis planner narrows to a subset of report tools, so a
// skipped aspect comes back filled with a sentinel summary (the synthesis
// prompt instructs the LLM to emit "Not fetched for this question." verbatim
// with a null label and empty bullets). Render nothing for those aspects
// rather than an empty stub section — the card shows only what was researched.
const NOT_FETCHED_SUMMARY = "not fetched for this question";

function aspectWasFetched(aspect: AspectView): boolean {
  return !aspect.summary.trim().toLowerCase().startsWith(NOT_FETCHED_SUMMARY);
}

function AspectBlock({ title, aspect }: { title: string; aspect: AspectView }) {
  if (!aspectWasFetched(aspect)) return null;
  return (
    <div>
      <div className="mb-1 flex items-baseline gap-2">
        <h4 className="font-mono text-[10px] uppercase tracking-wider text-zinc-400">
          {title}
        </h4>
        {aspect.label && (
          <span
            className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${ASPECT_LABEL_PILL[aspect.label]}`}
          >
            {aspect.label}
          </span>
        )}
      </div>
      <ProseBlock text={aspect.summary} />
      {aspect.supports.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-xs text-zinc-200">
          {aspect.supports.map((point, i) => (
            <li key={`s-${i}`} className="flex gap-1">
              <span className="text-emerald-500">+</span>
              <span className="min-w-0 flex-1">
                <ProseBlock text={point} />
              </span>
            </li>
          ))}
        </ul>
      )}
      {aspect.challenges.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-xs text-zinc-200">
          {aspect.challenges.map((point, i) => (
            <li key={`c-${i}`} className="flex gap-1">
              <span className="text-amber-500">·</span>
              <span className="min-w-0 flex-1">
                <ProseBlock text={point} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ThesisCard({
  ticker,
  thesis,
  stats,
  // QNT-229 #6: render verdict_rationale only when the narrative bubble is
  // absent (narrate degraded). Otherwise the bubble is the prose surface.
  showProse = true,
}: {
  ticker: string | null;
  thesis: ThesisPayload;
  stats: DoneEvent | null;
  showProse?: boolean;
}) {
  const confidencePct = stats
    ? Math.max(0, Math.min(100, Math.round(stats.confidence * 100)))
    : null;
  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="flex items-baseline justify-between gap-2 border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span>Thesis · {ticker ?? "session"} · this session</span>
        {stats && (
          <span className="text-zinc-500">
            {stats.tools_count} sources · {stats.citations_count} cited
          </span>
        )}
      </header>

      <div className="space-y-3 p-3">
        <AspectBlock title="Company" aspect={thesis.company} />
        <AspectBlock title="Fundamental" aspect={thesis.fundamental} />
        <AspectBlock title="Technical" aspect={thesis.technical} />
        <AspectBlock title="News" aspect={thesis.news} />

        <div className="rounded border border-zinc-800 bg-zinc-950/60 p-2">
          <div className="mb-1 flex items-center gap-2">
            <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Verdict
            </span>
            <span
              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${VERDICT_PILL[thesis.verdict]}`}
            >
              {thesis.verdict}
            </span>
          </div>
          {showProse && <ProseBlock text={thesis.verdict_rationale} />}
          {confidencePct !== null && (
            <div className="mt-2">
              <div className="mb-0.5 flex justify-between font-mono text-[10px] uppercase tracking-wider text-zinc-500">
                <span>Answer groundedness</span>
                <span>{confidencePct}%</span>
              </div>
              <div className="h-1 w-full overflow-hidden rounded bg-zinc-800">
                <div
                  className="h-full bg-sky-500"
                  style={{ width: `${confidencePct}%` }}
                />
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

// ─── Quick-fact compact card (QNT-149) ────────────────────────────────────
//
// The quick-fact path returns a short prose answer plus exactly one cited
// value. We render the answer the same way as thesis prose (so inline
// (source: …) chips work), and surface the structured cited value as a
// monospaced chip below the answer when present. The thesis card is
// intentionally absent for this run shape.

function QuickFactCard({
  ticker,
  quickFact,
  stats,
}: {
  ticker: string | null;
  quickFact: QuickFactPayload;
  stats: DoneEvent | null;
}) {
  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="flex items-baseline justify-between gap-2 border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span>Quick fact · {ticker ?? "session"}</span>
        {stats && (
          <span className="text-zinc-500">
            {stats.tools_count} sources · {stats.citations_count} cited
          </span>
        )}
      </header>

      <div className="space-y-2 p-3">
        <ProseBlock text={quickFact.answer} />
        {quickFact.cited_value && quickFact.source && (
          <div className="flex items-baseline gap-2">
            <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Value
            </span>
            <span className="rounded border border-zinc-700 bg-zinc-950 px-1.5 py-0.5 font-mono text-[11px] tabular-nums text-zinc-100">
              {quickFact.cited_value}
            </span>
            <span className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-zinc-400">
              {quickFact.source}
            </span>
          </div>
        )}
      </div>
    </section>
  );
}

// ─── Side-by-side comparison card (QNT-156) ───────────────────────────────
//
// Two columns, one per ticker, with a verbatim cited-values table beneath
// the prose summary. The differences paragraph renders as a single block
// below the two columns. ADR-003: every value here was copied verbatim from
// one ticker's reports — the rendering layer never computes deltas.

function ComparisonCard({
  comparison,
  stats,
  // QNT-229 #6: render the differences paragraph only when the narrative
  // bubble is absent (narrate degraded) — otherwise the bubble speaks it.
  showProse = true,
}: {
  comparison: ComparisonPayload;
  stats: DoneEvent | null;
  showProse?: boolean;
}) {
  const tickerHeader = comparison.sections.map((s) => s.ticker).join(" vs ");
  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="flex items-baseline justify-between gap-2 border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span>Comparison · {tickerHeader || "session"}</span>
        {stats && (
          <span className="text-zinc-500">
            {stats.tools_count} sources · {stats.citations_count} cited
          </span>
        )}
      </header>

      <div className="space-y-3 p-3">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {comparison.sections.map((section) => (
            <div
              key={section.ticker}
              className="space-y-3 rounded border border-zinc-800 bg-zinc-950/60 p-2"
            >
              <div className="font-mono text-[11px] uppercase tracking-wider text-zinc-300">
                {section.ticker}
              </div>
              <AspectBlock title="Company" aspect={section.company} />
              <AspectBlock title="Fundamental" aspect={section.fundamental} />
              <AspectBlock title="Technical" aspect={section.technical} />
              <AspectBlock title="News" aspect={section.news} />
            </div>
          ))}
        </div>

        {showProse && (
          <div className="rounded border border-zinc-800 bg-zinc-950/60 p-2">
            <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Differences
            </div>
            <ProseBlock text={comparison.differences} />
          </div>
        )}
      </div>
    </section>
  );
}

// ─── Lean N-way comparison card (QNT-224) ─────────────────────────────────
//
// The rich ComparisonCard above renders two fat aspect columns — that does not
// fit 3-4 tickers in the ~290-450px chat rail. The lean shape is a compact
// metrics table instead: tickers as columns, metrics as rows. N is capped at 4
// so the table is at most 5 columns (label + 4 tickers); overflow-x-auto saves
// the narrow md breakpoint and any long value. Every cell is a pre-formatted
// string copied verbatim from the API (ADR-003) — the panel computes nothing.

const LEAN_METRIC_ROWS: { key: "pe" | "rsi" | "net_margin" | "price"; label: string }[] = [
  { key: "pe", label: "P/E" },
  { key: "rsi", label: "RSI" },
  { key: "net_margin", label: "Net margin" },
  { key: "price", label: "Price" },
];

// QNT-224 follow-up: the interpretive verdicts (from the fundamental + technical
// reports) render as colored pills below the raw metrics, reusing the rich
// card's ASPECT_LABEL_PILL palette. null -> a muted dash.
const LEAN_LABEL_ROWS: {
  key: "valuation_label" | "trend_daily" | "trend_weekly";
  label: string;
}[] = [
  { key: "valuation_label", label: "Valuation" },
  { key: "trend_daily", label: "Trend (D)" },
  { key: "trend_weekly", label: "Trend (W)" },
];

function LeanComparisonCard({
  comparison,
  stats,
}: {
  comparison: LeanComparisonPayload;
  stats: DoneEvent | null;
}) {
  const { rows } = comparison;
  const tickerHeader = rows.map((r) => r.ticker).join(" vs ");
  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="flex items-baseline justify-between gap-2 border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span>Comparison · {tickerHeader || "session"}</span>
        {stats && (
          <span className="text-zinc-500">
            {stats.tools_count} sources · {stats.citations_count} cited
          </span>
        )}
      </header>

      <div className="overflow-x-auto p-3">
        <table className="w-full border-collapse font-mono text-[11px] tabular-nums">
          <thead>
            <tr className="text-zinc-300">
              <th className="px-2 py-1 text-left font-normal text-[10px] uppercase tracking-wider text-zinc-500">
                Metric
              </th>
              {rows.map((r) => (
                <th key={r.ticker} className="px-2 py-1 text-right font-semibold">
                  {r.ticker}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {LEAN_METRIC_ROWS.map(({ key, label }) => (
              <tr key={key} className="border-t border-zinc-800/60">
                <td className="px-2 py-1 text-left text-zinc-400">{label}</td>
                {rows.map((r) => (
                  <td key={r.ticker} className="px-2 py-1 text-right text-zinc-200">
                    {r[key]}
                  </td>
                ))}
              </tr>
            ))}
            {LEAN_LABEL_ROWS.map(({ key, label }, idx) => (
              <tr
                key={key}
                className={idx === 0 ? "border-t-2 border-zinc-700/80" : "border-t border-zinc-800/60"}
              >
                <td className="px-2 py-1 text-left text-zinc-400">{label}</td>
                {rows.map((r) => {
                  const value = r[key];
                  return (
                    <td key={r.ticker} className="px-2 py-1 text-right">
                      {value ? (
                        <span
                          className={`rounded border px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider ${ASPECT_LABEL_PILL[value]}`}
                        >
                          {value}
                        </span>
                      ) : (
                        <span className="text-zinc-600">—</span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ─── Conversational redirect card (QNT-156) ───────────────────────────────
//
// Used both for greetings/off-domain asks AND as the deterministic fallback
// when any other intent fails to produce a payload. Renders the prose answer
// + an optional bulleted suggestion list. Click a suggestion to drop it into
// the composer (parent-driven via ``onSuggestion``).

function ConversationalCard({
  conversational,
  onSuggestion,
}: {
  conversational: ConversationalPayload;
  onSuggestion: (q: string) => void;
}) {
  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="flex items-baseline justify-between gap-2 border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span>Analyst · session</span>
      </header>

      <div className="space-y-3 p-3">
        <ProseBlock text={conversational.answer} />
        {conversational.suggestions.length > 0 && (
          <div>
            <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              You could ask
            </div>
            <ul className="space-y-1">
              {conversational.suggestions.map((s, i) => (
                <li key={i}>
                  {/* Mid-conversation redirect: clicking auto-sends because
                      the user has already committed to asking. EmptyState
                      uses the same SuggestionButton but its parent prefills
                      the composer instead — different surface, different
                      contract. */}
                  <SuggestionButton text={s} onClick={() => onSuggestion(s)} />
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  );
}

// ─── Focused-analysis card (QNT-176) ──────────────────────────────────────
//
// One card shape covers all three focused intents (fundamental / technical /
// news). The ``focus`` discriminator drives the header label and
// the accent palette so a glance tells the user which read they got. The
// body fields are rendered the same way the comparison card renders its
// per-ticker section: prose summary with chips, a bullet list of key
// points, and a chip table of cited values.

const FOCUS_PILL: Record<FocusKind, { label: string; className: string }> = {
  fundamental: {
    label: "Fundamentals",
    className: "border-sky-700/40 bg-sky-900/20 text-sky-300",
  },
  technical: {
    label: "Technicals",
    className: "border-emerald-700/40 bg-emerald-900/20 text-emerald-300",
  },
  news: {
    label: "News",
    className: "border-amber-700/40 bg-amber-900/20 text-amber-300",
  },
};

function focusPill(focus: FocusKind): { label: string; className: string } {
  return (
    FOCUS_PILL[focus] ?? {
      label: focus,
      className: "border-zinc-700 bg-zinc-900/40 text-zinc-300",
    }
  );
}

function FocusedAnalysisCard({
  ticker,
  focused,
  stats,
  // QNT-229 #6: render the top-level summary only when the narrative bubble is
  // absent (narrate degraded). Key points, catalysts, cited values are
  // structured data and always render.
  showProse = true,
}: {
  ticker: string | null;
  focused: FocusedAnalysisPayload;
  stats: DoneEvent | null;
  showProse?: boolean;
}) {
  const pill = focusPill(focused.focus);
  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="flex items-baseline justify-between gap-2 border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span className="flex items-baseline gap-2">
          <span>Analysis · {ticker ?? "session"}</span>
          <span
            className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${pill.className}`}
          >
            {pill.label}
          </span>
        </span>
        {stats && (
          <span className="text-zinc-500">
            {stats.tools_count} sources · {stats.citations_count} cited
          </span>
        )}
      </header>

      <div className="space-y-3 p-3">
        {focused.verdict && (
          <div className="flex items-center gap-2">
            <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Verdict
            </span>
            <span
              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${ASPECT_LABEL_PILL[focused.verdict]}`}
            >
              {focused.verdict}
            </span>
          </div>
        )}
        {showProse && <ProseBlock text={focused.summary} />}

        {focused.focus === "news" && focused.existing_development && (
          <div>
            <h4 className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Running story
            </h4>
            <ProseBlock text={focused.existing_development} />
          </div>
        )}

        {focused.focus === "news" && focused.positive_catalysts.length > 0 && (
          <div>
            <h4 className="mb-1 font-mono text-[10px] uppercase tracking-wider text-emerald-400">
              Positive catalysts
            </h4>
            <ul className="space-y-0.5 text-xs text-zinc-200">
              {focused.positive_catalysts.map((c, i) => (
                <li key={`pc-${i}`} className="flex gap-1">
                  <span className="text-emerald-500">+</span>
                  <span className="min-w-0 flex-1">
                    <ProseBlock text={c} />
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {focused.focus === "news" && focused.negative_catalysts.length > 0 && (
          <div>
            <h4 className="mb-1 font-mono text-[10px] uppercase tracking-wider text-red-400">
              Negative catalysts
            </h4>
            <ul className="space-y-0.5 text-xs text-zinc-200">
              {focused.negative_catalysts.map((c, i) => (
                <li key={`nc-${i}`} className="flex gap-1">
                  <span className="text-red-500">-</span>
                  <span className="min-w-0 flex-1">
                    <ProseBlock text={c} />
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {focused.key_points.length > 0 && (
          <div>
            <h4 className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Key points
            </h4>
            <ol className="space-y-1 pl-4 text-xs text-zinc-200">
              {focused.key_points.map((point, i) => (
                <li key={i} className="list-decimal">
                  <ProseBlock text={point} />
                </li>
              ))}
            </ol>
          </div>
        )}

        {focused.cited_values.length > 0 && (
          <div className="rounded border border-zinc-800 bg-zinc-950/60 p-2">
            <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Cited values
            </div>
            <ul className="space-y-1">
              {focused.cited_values.map((kv, i) => (
                <li
                  key={i}
                  className="flex items-baseline justify-between gap-2 font-mono text-[10px]"
                >
                  <span className="uppercase tracking-wider text-zinc-500">{kv.label}</span>
                  <span className="flex items-baseline gap-1">
                    <span className="rounded border border-zinc-700 bg-zinc-950 px-1 py-px font-mono text-[10px] tabular-nums text-zinc-100">
                      {kv.value}
                    </span>
                    <span className="rounded border border-zinc-700 bg-zinc-900 px-1 py-px text-[9px] uppercase tracking-wide text-zinc-400">
                      {kv.source}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  );
}

// ─── Exploration-scan card (QNT-220 follow-up) ────────────────────────────
//
// Renders a broad anchored "what's interesting" scan: a headline of what
// stands out, cross-lens observation bullets, and verbatim cited-value chips.
// Deliberately verdict-free — a scan surfaces what is notable, it does not
// take a buy/sell stance — and carries no forward "watch next" (no report
// exposes dated catalysts to copy from). The chip table mirrors the focused
// card's cited-values block.

function ExplorationCard({
  ticker,
  exploration,
  stats,
  // QNT-229 #6: render the headline only when the narrative bubble is absent
  // (narrate degraded). Observations + cited values always render.
  showProse = true,
}: {
  ticker: string | null;
  exploration: ExplorationAnswerPayload;
  stats: DoneEvent | null;
  showProse?: boolean;
}) {
  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="flex items-baseline justify-between gap-2 border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span className="flex items-baseline gap-2">
          <span>Scan · {ticker ?? "session"}</span>
          <span className="rounded border border-violet-700/40 bg-violet-900/20 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-violet-300">
            Exploration
          </span>
        </span>
        {stats && (
          <span className="text-zinc-500">
            {stats.tools_count} sources · {stats.citations_count} cited
          </span>
        )}
      </header>

      <div className="space-y-3 p-3">
        {showProse && <ProseBlock text={exploration.headline} />}

        {exploration.observations.length > 0 && (
          <div>
            <h4 className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              What stands out
            </h4>
            <ul className="space-y-0.5 text-xs text-zinc-200">
              {exploration.observations.map((o, i) => (
                <li key={i} className="flex gap-1">
                  <span className="text-violet-500">·</span>
                  <span className="min-w-0 flex-1">
                    <ProseBlock text={o} />
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {exploration.cited_values.length > 0 && (
          <div className="rounded border border-zinc-800 bg-zinc-950/60 p-2">
            <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Cited values
            </div>
            <ul className="space-y-1">
              {exploration.cited_values.map((kv, i) => (
                <li
                  key={i}
                  className="flex items-baseline justify-between gap-2 font-mono text-[10px]"
                >
                  <span className="uppercase tracking-wider text-zinc-500">{kv.label}</span>
                  <span className="flex items-baseline gap-1">
                    <span className="rounded border border-zinc-700 bg-zinc-950 px-1 py-px font-mono text-[10px] tabular-nums text-zinc-100">
                      {kv.value}
                    </span>
                    <span className="rounded border border-zinc-700 bg-zinc-900 px-1 py-px text-[9px] uppercase tracking-wide text-zinc-400">
                      {kv.source}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  );
}

// ─── Retrieved sources (QNT-226) ──────────────────────────────────────────
//
// Provenance for the agent's semantic news search: the articles RAG actually
// surfaced this turn, shown as a compact clickable list under the analyst
// voice. On a targeted news ask the focused-news card is dropped (the voice
// answers it), so this list is the structured surface that shows the user
// WHICH headlines grounded the answer. Mirrors the external-link idiom in
// ticker/news-card.tsx (new tab, noopener).

function RetrievedSources({ sources }: { sources: RetrievedSource[] }) {
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
          <li key={`${src.url || src.headline}-${i}`}>
            {src.url ? (
              <a
                href={src.url}
                target="_blank"
                rel="noopener noreferrer"
                className="group flex flex-col rounded px-1 py-1 transition hover:bg-zinc-900 focus-visible:bg-zinc-900 focus-visible:outline-none"
              >
                <span className="text-xs text-zinc-200 group-hover:text-emerald-300">
                  {src.headline}
                </span>
                <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">
                  {[src.source, src.date].filter(Boolean).join(" · ")}
                </span>
              </a>
            ) : (
              <div className="flex flex-col px-1 py-1">
                <span className="text-xs text-zinc-200">{src.headline}</span>
                <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">
                  {[src.source, src.date].filter(Boolean).join(" · ")}
                </span>
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

// ─── Run renderer (one user prompt → one streamed answer) ────────────────

function RunBlock({
  run,
  onSuggestion,
}: {
  run: ChatRun;
  onSuggestion: (q: string) => void;
}) {
  const proseText = run.proseChunks.join("");
  const isStreaming = run.status === "streaming";
  const groundingPct =
    typeof run.stats?.grounding_rate === "number"
      ? Math.max(0, Math.min(100, Math.round(run.stats.grounding_rate * 100)))
      : null;
  const showGroundingWarning = groundingPct !== null && groundingPct < 100;
  // Hide free-form prose when the run produced any structured card —
  // each card renders its own prose with chips. Only show standalone
  // prose when the run is mid-stream and no card has arrived yet.
  // ADR-014 §4: each card renders only when its payload arrived.
  const hasCard =
    run.thesis !== null ||
    run.quickFact !== null ||
    run.comparison !== null ||
    run.comparisonLean !== null ||
    run.conversational !== null ||
    run.focused !== null ||
    run.exploration !== null;
  const showStandaloneProse = !hasCard && proseText;
  // QNT-229 #2a: composing indicator in the voice slot below the card.
  // `isComposing` is the timing predicate (intent known, tools done or a
  // no-tool short-circuit, nothing streamed, still live); it fires for every
  // intent. The two extra gates hide it the instant any content begins:
  // `!proseText` for the prose-reply paths (conversational / redirect stream
  // via prose_chunk) and `run.conversational === null` for the redirect/clarify
  // card. It also naturally ends when narration starts (narrative non-empty
  // fails isComposing) — so the spinner transitions in place to the bubble.
  // QNT-232 #3: quick_fact skips narrate, so no bubble follows its card. Once
  // the card lands the voice slot has nothing more to fill, so suppress the
  // composing shimmer to avoid a flash between card arrival and the run ending.
  // Scoped to the quick_fact intent — followup reuses the QuickFactCard but
  // still narrates, so it keeps composing until its narrative starts.
  const quickFactCardLanded = run.intent === "quick_fact" && run.quickFact !== null;
  const composing =
    !proseText &&
    run.conversational === null &&
    !quickFactCardLanded &&
    isComposing({
      status: run.status,
      intent: run.intent,
      toolRows: run.toolRows,
      narrative: run.narrative,
    });
  // QNT-229 follow-up: demotable card prose stays hidden while streaming so the
  // early card does not show fallback text and then retract it when narration
  // starts. Once done, it renders only if narrate degraded.
  const cardProse = showCardProse({
    status: run.status,
    narrative: run.narrative,
  });

  return (
    <article className="space-y-2 border-b border-zinc-800 px-3 py-3">
      {/* User prompt bubble */}
      <div className="flex flex-col gap-0.5">
        <div className="flex items-baseline justify-between font-mono text-[10px] uppercase tracking-wider text-zinc-500">
          <span>You</span>
          <span>{new Date(run.startedAt).toLocaleTimeString()}</span>
        </div>
        <p className="text-xs text-zinc-100">{run.prompt}</p>
      </div>

      {/* Tool-call rows */}
      {run.toolRows.length > 0 && (
        <div className="space-y-0.5 rounded bg-zinc-950/40 p-2">
          {run.toolRows.map((row, i) => (
            <ToolCallRow key={`${row.name}-${i}`} row={row} />
          ))}
        </div>
      )}

      {/* Errors (terminal) */}
      {run.errors.map((err, i) => (
        <div
          key={i}
          role="alert"
          className="rounded border border-red-700/40 bg-red-900/20 px-2 py-1 font-mono text-[11px] text-red-300"
        >
          {err.detail}
        </div>
      ))}

      {/* Streamed prose (only when no card has arrived yet) */}
      {showStandaloneProse && <ProseBlock text={proseText} />}

      {/* QNT-156: comparison card — renders when intent=comparison */}
      {run.comparison && (
        <ComparisonCard comparison={run.comparison} stats={run.stats} showProse={cardProse} />
      )}
      {run.comparisonLean && (
        <LeanComparisonCard comparison={run.comparisonLean} stats={run.stats} />
      )}

      {/* QNT-149: quick-fact card — renders when intent=quick_fact */}
      {run.quickFact && (
        <QuickFactCard ticker={run.ticker} quickFact={run.quickFact} stats={run.stats} />
      )}

      {/* QNT-176: focused-analysis card — renders when intent ∈
        {fundamental, technical, news} */}
      {run.focused && (
        <FocusedAnalysisCard
          ticker={run.ticker}
          focused={run.focused}
          stats={run.stats}
          showProse={cardProse}
        />
      )}

      {/* QNT-220 follow-up: exploration-scan card — renders when
        intent=exploration (broad anchored "what's interesting" scans) */}
      {run.exploration && (
        <ExplorationCard
          ticker={run.ticker}
          exploration={run.exploration}
          stats={run.stats}
          showProse={cardProse}
        />
      )}

      {/* Structured thesis (only when intent=thesis) */}
      {run.thesis && (
        <ThesisCard
          ticker={run.ticker}
          thesis={run.thesis}
          stats={run.stats}
          showProse={cardProse}
        />
      )}

      {/* QNT-156: conversational redirect — also serves as the
        deterministic fallback when any other intent failed to produce a
        primary payload. The hint below the suggestions explains where
        the redirect came from. */}
      {run.conversational && (
        <ConversationalCard
          conversational={run.conversational}
          onSuggestion={onSuggestion}
        />
      )}

      {/* Analyst-voice slot — BELOW the structured card. The card anchors on
        top the instant it lands (QNT-229 #2b early emit); everything else grows
        DOWNWARD beneath it, so the card is never shoved by content appearing
        above it. QNT-229 #2a: this slot holds the composing pixel-spinner during
        the synthesize window, then becomes the streaming narrative bubble
        (QNT-211) once narration starts — the same slot, so the spinner→voice
        transition is in place. For the followup narrative-only path (no card)
        the bubble is the only surface. */}
      {run.narrative ? (
        <NarrativeBubble text={run.narrative} />
      ) : composing ? (
        <ComposingBubble intent={run.intent} />
      ) : null}

      {/* QNT-226: provenance for the semantic news search. On a targeted news
        ask the focused-news card is dropped, so this clickable list is the
        structured surface showing which articles grounded the spoken answer. */}
      <RetrievedSources sources={run.retrievedSources} />

      {showGroundingWarning && (
        <div
          role="alert"
          className="rounded border border-amber-700/40 bg-amber-950/30 px-2 py-1.5 font-mono text-[10px] leading-relaxed text-amber-200"
        >
          Some numbers in this answer were not found in the supplied reports.
          Groundedness: {groundingPct}%. Verify before relying on them.
        </div>
      )}

      {/* Disclaimer footer (QNT-195) — shown once any result card is present.
        QNT-211: narrative-only followup runs surface no card but still
        carry analyst prose that can cite reports; trigger the disclaimer
        for those too. */}
      {hasAnswerSurface(run) && !isStreaming && (
          <p className="font-mono text-[10px] italic text-zinc-500">
            Informational only — not investment advice. Figures are from the
            supplied reports and may be stale. Groundedness is source support,
            not market-call probability; verify before acting.
          </p>
        )}

      {/* Terminal status footer. QNT-229: no "streaming…" line while the run is
        live — activity is already shown by the tool rows (gather), the composing
        box (synthesize), and the streaming bubble/prose (narration). The footer
        appears only on completion to carry the run summary / errored state. */}
      {!isStreaming && (
        <div className="flex items-baseline justify-end gap-2 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
          {run.status === "errored" ? (
            <span className="text-red-400">errored</span>
          ) : run.stats ? (
            <span>
              {run.stats.tools_count} tools · {run.stats.citations_count} citations · done
            </span>
          ) : (
            <span>done</span>
          )}
        </div>
      )}
    </article>
  );
}

// ─── Health-provenance source list (composer placeholder) ────────────────

function useHealthSources(): string[] {
  const [sources, setSources] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE_URL}/api/v1/health`, { cache: "no-store" })
      .then((r) => (r.ok ? (r.json() as Promise<HealthResponse>) : null))
      .then((body) => {
        if (cancelled || !body?.provenance?.sources) return;
        setSources(body.provenance.sources);
      })
      .catch(() => {
        // /health unreachable — composer falls back to no-source placeholder.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return sources;
}

// ─── SSE consumption ──────────────────────────────────────────────────────

// QNT-161: friendly fallback when the backend returns a non-2xx that the
// server side wasn't able to dress up as a conversational redirect (e.g.
// SlowAPI 429 — issued before the SSE generator gets to run). The chat
// panel surfaces this as the user-facing detail in the run's error rail.
const RATE_LIMIT_REDIRECT =
  "You've hit the demo rate limit on this IP. The portfolio runs on a free " +
  "LLM tier; the cap protects daily uptime for other visitors. Try again in " +
  "a moment, or fork the repo to run the agent against your own keys.";

async function consumeChatStream(
  body: { ticker: string; message: string; thread_id?: string },
  onEvent: (event: string, data: unknown) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/api/v1/agent/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(body),
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    // QNT-161: 429 from the SlowAPI rate limiter ships a JSON body with a
    // friendly detail string. Surface it AS-IS so the panel shows the
    // server's chosen copy (and a Retry-After hint when present); other
    // non-2xxs fall back to a generic transport error.
    if (response.status === 429) {
      let detail = RATE_LIMIT_REDIRECT;
      let retryHint = "";
      try {
        const body = (await response.json()) as { detail?: string; retry_after?: string };
        if (typeof body.detail === "string" && body.detail.length > 0) detail = body.detail;
        if (typeof body.retry_after === "string") retryHint = ` (try again in ${body.retry_after}s)`;
      } catch {
        // Body wasn't JSON — keep the default.
      }
      throw new Error(`${detail}${retryHint}`);
    }
    const text = await response.text().catch(() => "");
    throw new Error(`HTTP ${response.status}: ${text || "request failed"}`);
  }
  for await (const frame of parseSseStream(response, signal)) {
    let parsed: unknown = null;
    try {
      parsed = JSON.parse(frame.data);
    } catch {
      // Ignore malformed frames — the server contract emits valid JSON, but a
      // proxy that splits a UTF-8 codepoint mid-frame would surface here.
      continue;
    }
    onEvent(frame.event, parsed);
  }
}

// ─── Top-level panel ──────────────────────────────────────────────────────

export function ChatPanel() {
  const pathname = usePathname();
  const ticker = activeTickerFromPath(pathname);
  const sources = useHealthSources();
  const [runs, setRuns] = useState<ChatRun[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  // QNT-178: composer is now controlled from here so EmptyState suggestion
  // clicks can prefill the textarea. ``focusKey`` is bumped after a prefill
  // to request focus inside Composer (skipped on initial render so opening
  // the page doesn't steal focus).
  // Initialised at 0 so the focus useEffect inside Composer can skip the
  // first render — prefillComposer increments the counter, never resets it.
  const [composerInput, setComposerInput] = useState("");
  const [composerFocusKey, setComposerFocusKey] = useState(0);
  // QNT-209: per-ticker thread map. One thread_id per ticker per ChatPanel
  // mount; lifetime = this React component state (NOT localStorage, NOT
  // sessionStorage — both survive refresh and would break the desired
  // "refresh = restart" UX). Switching tickers within the same session
  // gives each ticker its own continuity; switching back to a prior ticker
  // reuses its original thread_id so a followup question still has the
  // earlier turn to reason over.
  const [threadIds, setThreadIds] = useState<Record<string, string>>({});

  // Clear the composer when the active ticker changes. Prefilled suggestions
  // ("Technical analysis of TSLA") embed the ticker by name, so leaving the
  // text behind on /ticker/AAPL would let the user accidentally Send a TSLA
  // question against the AAPL run path. Pre-existing typed text gets cleared
  // too — the cost (re-typing 30 chars) is small vs. cross-ticker leak risk.
  // React-recommended "adjust state during render" pattern (avoids the
  // setState-in-useEffect anti-pattern); fires synchronously, no extra commit.
  // https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes
  const [prevTicker, setPrevTicker] = useState(ticker);
  if (prevTicker !== ticker) {
    setPrevTicker(ticker);
    setComposerInput("");
  }

  const prefillComposer = useCallback((q: string) => {
    setComposerInput(q);
    setComposerFocusKey((k) => k + 1);
  }, []);

  // Auto-scroll to the latest run as events arrive.
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    scrollerRef.current?.scrollTo({
      top: scrollerRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [runs]);

  const updateRun = useCallback((id: string, patch: (run: ChatRun) => ChatRun) => {
    setRuns((prev) => prev.map((r) => (r.id === id ? patch(r) : r)));
  }, []);

  const startRun = useCallback(
    (prompt: string) => {
      // The endpoint requires a valid ticker. If the user is on the landing
      // route we surface a friendly error instead of crashing the panel.
      if (!ticker) {
        const id = crypto.randomUUID();
        const errored: ChatRun = {
          id,
          ticker: null,
          prompt,
          startedAt: Date.now(),
          status: "errored",
          intent: null,
          toolRows: [],
          proseChunks: [],
          narrative: "",
          thesis: null,
          quickFact: null,
          comparison: null,
          comparisonLean: null,
          conversational: null,
          focused: null,
          exploration: null,
          retrievedSources: [],
          errors: [
            {
              detail: "Pick a ticker from the watchlist before asking the analyst.",
              code: "no-ticker",
            },
          ],
          stats: null,
        };
        setRuns((prev) => [...prev.slice(-MAX_RUNS + 1), errored]);
        return;
      }

      const id = crypto.randomUUID();
      const run: ChatRun = {
        id,
        ticker,
        prompt,
        startedAt: Date.now(),
        status: "streaming",
        intent: null,
        toolRows: [],
        proseChunks: [],
        narrative: "",
        thesis: null,
        quickFact: null,
        comparison: null,
        comparisonLean: null,
        conversational: null,
        focused: null,
        exploration: null,
        retrievedSources: [],
        errors: [],
        stats: null,
      };
      setRuns((prev) => [...prev.slice(-MAX_RUNS + 1), run]);

      // Cancel any in-flight stream — only one run streams at a time per
      // session-scope. A future revision could allow concurrent runs but the
      // SSE backpressure model is simpler with one.
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      // QNT-209: lazily mint a thread_id for this ticker if we haven't yet,
      // otherwise reuse the existing one. Stored via setThreadIds so a
      // navigation away and back lands on the same id (intentional: the
      // user expects continuity within one session).
      const existingThreadId = threadIds[ticker];
      const threadId = existingThreadId ?? crypto.randomUUID();
      if (!existingThreadId) {
        setThreadIds((prev) => ({ ...prev, [ticker]: threadId }));
      }

      consumeChatStream(
        {
          ticker,
          message: prompt,
          thread_id: threadId,
        },
        (event, data) => {
          if (event === "tool_call") {
            const ev = data as ToolCallEvent;
            updateRun(id, (r) => ({
              ...r,
              toolRows: [...r.toolRows, { ...ev }],
            }));
          } else if (event === "tool_result") {
            const ev = data as ToolResultEvent;
            updateRun(id, (r) => ({
              ...r,
              toolRows: r.toolRows.map((row) =>
                row.name === ev.name && !row.result ? { ...row, result: ev } : row,
              ),
            }));
          } else if (event === "prose_chunk") {
            const ev = data as ProseChunkEvent;
            updateRun(id, (r) => ({
              ...r,
              proseChunks: [...r.proseChunks, ev.delta],
            }));
          } else if (event === "narrative_chunk") {
            const ev = data as NarrativeChunkEvent;
            updateRun(id, (r) => ({
              ...r,
              narrative: r.narrative + ev.delta,
            }));
          } else if (event === "intent") {
            const ev = data as IntentEvent;
            updateRun(id, (r) => ({ ...r, intent: ev.intent }));
          } else if (event === "thesis") {
            const ev = data as ThesisPayload;
            updateRun(id, (r) => ({ ...r, thesis: ev }));
          } else if (event === "quick_fact") {
            const ev = data as QuickFactPayload;
            updateRun(id, (r) => ({ ...r, quickFact: ev }));
          } else if (event === "comparison") {
            const ev = data as ComparisonPayload;
            updateRun(id, (r) => ({ ...r, comparison: ev }));
          } else if (event === "comparison_lean") {
            const ev = data as LeanComparisonPayload;
            updateRun(id, (r) => ({ ...r, comparisonLean: ev }));
          } else if (event === "conversational") {
            const ev = data as ConversationalPayload;
            updateRun(id, (r) => ({ ...r, conversational: ev }));
          } else if (event === "focused") {
            const ev = data as FocusedAnalysisPayload;
            updateRun(id, (r) => ({ ...r, focused: ev }));
          } else if (event === "exploration") {
            const ev = data as ExplorationAnswerPayload;
            updateRun(id, (r) => ({ ...r, exploration: ev }));
          } else if (event === "retrieved_sources") {
            const ev = data as RetrievedSourcesEvent;
            updateRun(id, (r) => ({ ...r, retrievedSources: ev.sources }));
          } else if (event === "done") {
            const ev = data as DoneEvent;
            const unsupported = ev.grounding_unsupported ?? [];
            updateRun(id, (r) => ({
              ...r,
              stats: ev,
              narrative: redactUnsupportedNumbers(r.narrative, unsupported),
              proseChunks: r.proseChunks.map((chunk) =>
                redactUnsupportedNumbers(chunk, unsupported),
              ),
              // A run is "errored" only when it hit a terminal error AND
              // produced no answer surface at all (QNT-226: retrieved sources
              // count as a surface — see hasAnswerSurface).
              status: r.errors.length > 0 && !hasAnswerSurface(r) ? "errored" : "done",
            }));
          } else if (event === "error") {
            const ev = data as ChatErrorEvent;
            updateRun(id, (r) => ({ ...r, errors: [...r.errors, ev] }));
          }
        },
        controller.signal,
      ).catch((err: unknown) => {
        if (controller.signal.aborted) return; // user-initiated cancel
        const detail = err instanceof Error ? err.message : "unknown stream error";
        updateRun(id, (r) => ({
          ...r,
          errors: [...r.errors, { detail, code: "transport-failed" }],
          status: "errored",
        }));
      });
    },
    [ticker, updateRun, threadIds],
  );

  // Cancel any in-flight stream when the panel unmounts.
  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const isStreaming = runs.some((r) => r.status === "streaming");

  return (
    <aside
      aria-label="Agent chat"
      className="flex h-full flex-col border-l border-zinc-800 bg-zinc-950 text-zinc-100"
    >
      <header className="flex flex-col gap-1 border-b border-zinc-800 px-4 py-3 font-mono text-xs uppercase tracking-wider">
        <div className="flex items-baseline justify-between">
          <span className="text-zinc-300">
            Analyst · {ticker ?? "session"}
          </span>
        </div>
        {/* QNT-161: demo-limits hint. Sets expectations BEFORE the user
            hits a 429 — recruiters see the cap is intentional, not a
            broken endpoint, and the bounce-rate stays low. The exact
            numbers come from packages/shared/src/shared/config.py
            (CHAT_RATE_LIMIT) — keep them aligned by hand for now;
            QNT-86 / a future "/api/v1/limits" endpoint can drive this
            from the server.
            QNT-178: dropped the LangGraph / Cited header pills — both
            were decorative; the trust line below is the canonical
            advertisement of what's behind the demo. */}
        <span className="text-[11px] normal-case tracking-normal text-zinc-500">
          demo: ~30 queries/hour per visitor · powered by Groq free tier
        </span>
      </header>

      <div ref={scrollerRef} className="min-h-0 flex-1 overflow-y-auto">
        {runs.length === 0 ? (
          <EmptyState ticker={ticker} onSuggestion={prefillComposer} />
        ) : (
          runs.map((run) => (
            <RunBlock
              key={run.id}
              run={run}
              onSuggestion={(q) => startRun(q)}
            />
          ))
        )}
      </div>

      <Composer
        ticker={ticker}
        sources={sources}
        disabled={isStreaming}
        value={composerInput}
        onChange={setComposerInput}
        onSubmit={startRun}
        focusKey={composerFocusKey}
      />
    </aside>
  );
}
