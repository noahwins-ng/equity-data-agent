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
  type ChatErrorEvent,
  type ComparisonPayload,
  type ConversationalPayload,
  type DoneEvent,
  type HealthResponse,
  type Intent,
  type IntentEvent,
  type ProseChunkEvent,
  type QuickFactPayload,
  type ThesisPayload,
  type ToolCallEvent,
  type ToolResultEvent,
} from "@/lib/api";
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
  thesis: ThesisPayload | null;
  quickFact: QuickFactPayload | null;
  comparison: ComparisonPayload | null;
  conversational: ConversationalPayload | null;
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

// ─── Composer — input + tools/cite toggles + send ─────────────────────────

function Composer({
  ticker,
  sources,
  disabled,
  onSubmit,
}: {
  ticker: string | null;
  sources: string[];
  disabled: boolean;
  onSubmit: (input: string, toolsEnabled: boolean, citeSources: boolean) => void;
}) {
  const [input, setInput] = useState("");
  const [toolsEnabled, setToolsEnabled] = useState(true);
  const [citeSources, setCiteSources] = useState(true);

  const placeholder = useMemo(() => {
    const focus = ticker ? ticker : "the watchlist";
    if (sources.length === 0) {
      return `Ask the analyst about ${focus}...`;
    }
    return `Ask the analyst about ${focus}... (cites ${sources.join(", ")})`;
  }, [ticker, sources]);

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = input.trim();
    if (!trimmed || disabled) return;
    onSubmit(trimmed, toolsEnabled, citeSources);
    setInput("");
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
        <button
          type="button"
          onClick={() => setToolsEnabled((v) => !v)}
          aria-pressed={toolsEnabled}
          className={`rounded border px-1.5 py-0.5 font-mono transition ${
            toolsEnabled
              ? "border-zinc-600 bg-zinc-800 text-zinc-100"
              : "border-zinc-800 bg-zinc-900/60 text-zinc-500"
          }`}
        >
          tools {toolsEnabled ? "on" : "off"}
        </button>
        <button
          type="button"
          onClick={() => setCiteSources((v) => !v)}
          aria-pressed={citeSources}
          className={`rounded border px-1.5 py-0.5 font-mono transition ${
            citeSources
              ? "border-zinc-600 bg-zinc-800 text-zinc-100"
              : "border-zinc-800 bg-zinc-900/60 text-zinc-500"
          }`}
        >
          cite {citeSources ? "on" : "off"}
        </button>
      </div>
      <div className="flex items-end gap-2">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
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
          disabled={disabled || !input.trim()}
          className="h-8 rounded bg-emerald-600 px-3 text-[10px] font-semibold uppercase tracking-wider text-white transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Send
        </button>
      </div>
    </form>
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

// ─── Structured thesis card ───────────────────────────────────────────────

const STANCE_PILL: Record<ThesisPayload["verdict_stance"], string> = {
  constructive: "border-emerald-700/40 bg-emerald-900/20 text-emerald-300",
  cautious: "border-amber-700/40 bg-amber-900/20 text-amber-300",
  negative: "border-red-700/40 bg-red-900/20 text-red-300",
  mixed: "border-zinc-700 bg-zinc-900/40 text-zinc-300",
};

function ThesisCard({
  ticker,
  thesis,
  stats,
}: {
  ticker: string | null;
  thesis: ThesisPayload;
  stats: DoneEvent | null;
}) {
  const confidencePct = stats ? Math.max(0, Math.min(100, Math.round(stats.confidence * 100))) : null;
  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40">
      <header className="flex items-baseline justify-between gap-2 border-b border-zinc-800 px-3 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span>
          Thesis · {ticker ?? "session"} · this session
        </span>
        {stats && (
          <span className="text-zinc-500">
            {stats.tools_count} sources · {stats.citations_count} cited
          </span>
        )}
      </header>

      <div className="space-y-3 p-3">
        <ProseBlock text={thesis.setup} />

        <div>
          <h4 className="mb-1 font-mono text-[10px] uppercase tracking-wider text-emerald-400">
            ▲ Bull Case
          </h4>
          {thesis.bull_case.length === 0 ? (
            <p className="text-xs italic text-zinc-500">
              No bull case supported by the reports.
            </p>
          ) : (
            <ol className="space-y-1 pl-4 text-xs text-zinc-200">
              {thesis.bull_case.map((point, i) => (
                <li key={i} className="list-decimal">
                  <ProseBlock text={point} />
                </li>
              ))}
            </ol>
          )}
        </div>

        <div>
          <h4 className="mb-1 font-mono text-[10px] uppercase tracking-wider text-red-400">
            ▼ Bear Case
          </h4>
          {thesis.bear_case.length === 0 ? (
            <p className="text-xs italic text-zinc-500">
              No bear case supported by the reports.
            </p>
          ) : (
            <ol className="space-y-1 pl-4 text-xs text-zinc-200">
              {thesis.bear_case.map((point, i) => (
                <li key={i} className="list-decimal">
                  <ProseBlock text={point} />
                </li>
              ))}
            </ol>
          )}
        </div>

        <div className="rounded border border-zinc-800 bg-zinc-950/60 p-2">
          <div className="mb-1 flex items-center gap-2">
            <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-500">
              Verdict
            </span>
            <span
              className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider ${STANCE_PILL[thesis.verdict_stance]}`}
            >
              {thesis.verdict_stance}
            </span>
          </div>
          <ProseBlock text={thesis.verdict_action} />
          {confidencePct !== null && (
            <div className="mt-2">
              <div className="mb-0.5 flex justify-between font-mono text-[10px] uppercase tracking-wider text-zinc-500">
                <span>Confidence</span>
                <span>{confidencePct}%</span>
              </div>
              <div className="h-1 w-full overflow-hidden rounded bg-zinc-800">
                <div
                  className="h-full bg-emerald-500"
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
}: {
  comparison: ComparisonPayload;
  stats: DoneEvent | null;
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
              className="rounded border border-zinc-800 bg-zinc-950/60 p-2"
            >
              <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
                {section.ticker}
              </div>
              <ProseBlock text={section.summary} />
              {section.key_values.length > 0 && (
                <ul className="mt-2 space-y-1">
                  {section.key_values.map((kv, i) => (
                    <li
                      key={`${section.ticker}-${i}`}
                      className="flex items-baseline justify-between gap-2 font-mono text-[10px]"
                    >
                      <span className="uppercase tracking-wider text-zinc-500">
                        {kv.label}
                      </span>
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
              )}
            </div>
          ))}
        </div>

        <div className="rounded border border-zinc-800 bg-zinc-950/60 p-2">
          <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
            Differences
          </div>
          <ProseBlock text={comparison.differences} />
        </div>
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
                  <button
                    type="button"
                    onClick={() => onSuggestion(s)}
                    className="w-full rounded border border-zinc-800 bg-zinc-950/60 px-2 py-1 text-left font-mono text-[11px] text-zinc-300 transition hover:border-zinc-600 hover:text-zinc-100"
                  >
                    {s}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
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
  // Hide free-form prose when the run produced any structured card —
  // each card renders its own prose with chips. Only show standalone
  // prose when the run is mid-stream and no card has arrived yet.
  // ADR-014 §4: each card renders only when its payload arrived.
  const hasCard =
    run.thesis !== null ||
    run.quickFact !== null ||
    run.comparison !== null ||
    run.conversational !== null;
  const showStandaloneProse = !hasCard && proseText;

  // Streaming label — match the user's chosen layout so the spinner names
  // the right shape.
  const streamingLabel: Record<Intent, string> = {
    thesis: "thesis…",
    quick_fact: "quick fact…",
    comparison: "comparison…",
    conversational: "reply…",
  };

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
        <ComparisonCard comparison={run.comparison} stats={run.stats} />
      )}

      {/* QNT-149: quick-fact card — renders when intent=quick_fact */}
      {run.quickFact && (
        <QuickFactCard ticker={run.ticker} quickFact={run.quickFact} stats={run.stats} />
      )}

      {/* Structured thesis (only when intent=thesis) */}
      {run.thesis && (
        <ThesisCard ticker={run.ticker} thesis={run.thesis} stats={run.stats} />
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

      {/* Status footer */}
      <div className="flex items-baseline justify-end gap-2 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
        {isStreaming ? (
          <span className="text-emerald-400">
            streaming {streamingLabel[run.intent ?? "thesis"]}
          </span>
        ) : run.status === "errored" ? (
          <span className="text-red-400">errored</span>
        ) : run.stats ? (
          <span>
            {run.stats.tools_count} tools · {run.stats.citations_count} citations · done
          </span>
        ) : (
          <span>done</span>
        )}
      </div>
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

async function consumeChatStream(
  body: { ticker: string; message: string; tools_enabled: boolean; cite_sources: boolean },
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
    (prompt: string, toolsEnabled: boolean, citeSources: boolean) => {
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
          thesis: null,
          quickFact: null,
          comparison: null,
          conversational: null,
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
        thesis: null,
        quickFact: null,
        comparison: null,
        conversational: null,
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

      consumeChatStream(
        {
          ticker,
          message: prompt,
          tools_enabled: toolsEnabled,
          cite_sources: citeSources,
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
          } else if (event === "conversational") {
            const ev = data as ConversationalPayload;
            updateRun(id, (r) => ({ ...r, conversational: ev }));
          } else if (event === "done") {
            const ev = data as DoneEvent;
            updateRun(id, (r) => ({
              ...r,
              stats: ev,
              status:
                r.errors.length > 0 &&
                !r.thesis &&
                !r.quickFact &&
                !r.comparison &&
                !r.conversational
                  ? "errored"
                  : "done",
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
    [ticker, updateRun],
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
      <header className="flex items-baseline justify-between border-b border-zinc-800 px-4 py-3 font-mono text-[10px] uppercase tracking-wider">
        <span className="text-zinc-300">
          Analyst · {ticker ?? "session"}
        </span>
        <span className="flex gap-1">
          <span className="rounded border border-zinc-700 bg-zinc-900/60 px-1 py-0.5 text-zinc-400">
            LangGraph
          </span>
          <span className="rounded border border-zinc-700 bg-zinc-900/60 px-1 py-0.5 text-zinc-400">
            Cited
          </span>
        </span>
      </header>

      <div ref={scrollerRef} className="min-h-0 flex-1 overflow-y-auto">
        {runs.length === 0 ? (
          <div className="flex h-full items-center justify-center px-6 text-center">
            <p className="text-xs text-zinc-500">
              Ask a question to start a research session.
              {ticker
                ? ` Active ticker: ${ticker}.`
                : " Pick a ticker from the watchlist first."}
            </p>
          </div>
        ) : (
          runs.map((run) => (
            <RunBlock
              key={run.id}
              run={run}
              onSuggestion={(q) => startRun(q, true, true)}
            />
          ))
        )}
      </div>

      <Composer
        ticker={ticker}
        sources={sources}
        disabled={isStreaming}
        onSubmit={startRun}
      />
    </aside>
  );
}
