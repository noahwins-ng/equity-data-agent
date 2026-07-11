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
 *
 * QNT-253: the run renderer and its 9 card components live under
 * components/chat/; this file is the container — SSE consumption, the run
 * tape, the composer, and the empty state.
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
  type ExplorationAnswerPayload,
  type FocusedAnalysisPayload,
  type HealthResponse,
  type IntentEvent,
  type LeanComparisonPayload,
  type NarrativeChunkEvent,
  type PlanRationaleEvent,
  type ProseChunkEvent,
  type QuickFactPayload,
  type RetrievedSourcesEvent,
  type ThesisPayload,
  type ToolCallEvent,
  type ToolResultEvent,
} from "@/lib/api";
import { parseSseStream } from "@/lib/sse";
import { announceableAnswer, bindToolResult, hasAnswerSurface } from "./chat-run";
import {
  annotateUnsupportedDeep,
  annotateUnsupportedNumbers,
} from "./chat/annotate-unsupported";
import { RunBlock } from "./chat/run-block";
import { SuggestionButton } from "./chat/suggestion-button";
import type { ChatRun } from "./chat/types";

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

// QNT-361 follow-up: grounding misses are annotated ("45%†"), no longer
// redacted to "[unsupported number]" — see annotate-unsupported.ts.

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
  contextTicker,
  onContextChipClick,
  sources,
  disabled,
  value,
  onChange,
  onSubmit,
  focusKey,
}: {
  ticker: string | null;
  // QNT-299: the ticker the agent actually anchored the LAST turn to (may
  // diverge from ``ticker``, the URL-page ticker, on a rebase). null before
  // any turn has completed.
  contextTicker: string | null;
  onContextChipClick: () => void;
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
      return `Ask the analyst about ${focus}…`;
    }
    return `Ask the analyst about ${focus}… (cites ${sources.join(", ")})`;
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
        {/* QNT-299: context-anchor chip -- what the agent resolved "it" to on
          the last completed turn. Surfaces a mis-anchored followup (rebase to
          a different ticker) BEFORE the answer arrives, instead of only after.
          Click prefills a ticker-switch starter phrase -- cheapest useful
          affordance, no ticker-picker UI. */}
        {contextTicker && (
          <button
            type="button"
            onClick={onContextChipClick}
            title="Click to switch the conversation to a different ticker"
            className="rounded border border-sky-700/40 bg-sky-900/20 px-1.5 py-0.5 font-mono text-sky-400 transition hover:bg-sky-900/40"
          >
            Context: {contextTicker}
          </button>
        )}
      </div>
      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          rows={2}
          placeholder={placeholder}
          aria-label="Message the analyst"
          disabled={disabled}
          className="min-h-[2.5rem] flex-1 resize-none rounded border border-zinc-800 bg-zinc-900 px-2 py-1.5 text-xs text-zinc-100 placeholder:text-zinc-600 focus-visible:border-zinc-600 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-emerald-500/60 disabled:opacity-60"
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
    // QNT-286: the landing (no-ticker) rail used to be a single centred line,
    // which read as an empty third of the screen on first load. Fill it with a
    // real empty-state — what the analyst is and what it can do — so the panel
    // looks intentional before any click. The capability list is illustrative,
    // not clickable: the composer is disabled until a ticker is picked, so the
    // closing line is the actionable cue. Mirrors the ticker EmptyState below.
    return (
      <div className="flex h-full flex-col justify-center gap-4 px-4 py-6">
        <div className="space-y-1">
          <p className="text-sm font-medium text-zinc-200">Research analyst</p>
          <p className="text-xs leading-relaxed text-zinc-500">
            An agent that reads pre-computed reports across the portfolio and
            synthesises an investment thesis — fundamentals, technicals, and
            news — with every claim cited to its source.
          </p>
        </div>
        <div className="space-y-1.5">
          <p className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">
            What you can ask
          </p>
          <ul className="space-y-1 text-xs text-zinc-400">
            {[
              "A balanced thesis with an Overweight / Neutral / Underweight call",
              "A focused read on the technicals",
              "A walk through the fundamentals",
              "The latest news and how it lands",
            ].map((line) => (
              <li key={line} className="flex gap-2">
                <span className="select-none text-zinc-600">·</span>
                <span className="min-w-0 flex-1">{line}</span>
              </li>
            ))}
          </ul>
        </div>
        <p className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">
          Pick a ticker from the watchlist to begin
        </p>
      </div>
    );
  }
  const suggestions = emptyStateSuggestions(ticker);
  // Vertically centre the empty-state cluster. The earlier top-anchor (audit
  // #21) was a reaction to dead space, but with no runs yet the panel is mostly
  // void below a top-clinging block — centering reads as a deliberate "start
  // here" focal point. This only affects runs.length === 0; once the first run
  // streams in, RunBlock takes over (top-anchored, grows downward), so there is
  // no jump-from-the-middle.
  return (
    <div className="flex h-full flex-col justify-center gap-4 px-4 py-6">
      <div className="space-y-1">
        <p className="text-sm font-medium text-zinc-200">Research {ticker}</p>
        <p className="text-xs leading-relaxed text-zinc-500">
          Ask the analyst for a balanced thesis, a focused read on fundamentals
          or technicals, or the latest news. Every answer cites the underlying
          reports.
        </p>
      </div>
      <div className="space-y-1.5">
        <p className="font-mono text-[10px] uppercase tracking-wider text-zinc-600">
          Try asking
        </p>
        <ul className="space-y-1">
          {suggestions.map((s) => (
            <li key={s}>
              <SuggestionButton text={s} onClick={() => onSuggestion(s)} />
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

// ─── QNT-247: debounced screen-reader announcement ────────────────────────
//
// Trailing debounce: as the answer text changes on every token, the timer keeps
// resetting; the settled value is committed only once the stream goes quiet for
// ANNOUNCE_DEBOUNCE_MS (or completes). That collapses the token storm into a
// single polite announcement instead of one utterance per token.
const ANNOUNCE_DEBOUNCE_MS = 600;

function useDebounced<T>(value: T, delayMs: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return settled;
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
  // QNT-245: one thread_id per ChatPanel mount, ticker-agnostic. A thread is a
  // CONVERSATION, not a ticker page. The answered subject is a per-turn
  // property (QNT-228 message-wins rebase + analysis_ticker), so a single
  // thread spans turns about different tickers without fragmenting on
  // navigation. ChatPanel is mounted in app/layout.tsx and does NOT remount on
  // route change, so this id naturally carries across cross-ticker navigation
  // and resets only on refresh/remount. Held in a ref (not state): it is never
  // rendered, only sent to the API, so a ref avoids needless re-renders and
  // dependency-array churn. NOT localStorage/sessionStorage by explicit choice
  // — both survive refresh and would break the desired "refresh = restart" UX.
  const threadIdRef = useRef<string | null>(null);

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
    // Honour prefers-reduced-motion: smooth-scroll is vestibular-trigger
    // motion, so fall back to an instant jump when the user has asked for
    // reduced motion (QNT-249 — the pixel-spinner already does this).
    const prefersReducedMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    scrollerRef.current?.scrollTo({
      top: scrollerRef.current.scrollHeight,
      behavior: prefersReducedMotion ? "auto" : "smooth",
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
          planRationale: null,
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
        planRationale: null,
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

      // QNT-245: lazily mint the per-mount thread_id on first send, then reuse
      // it for every subsequent turn this mount — regardless of which ticker
      // page is active. One conversation, ticker-agnostic.
      threadIdRef.current ??= crypto.randomUUID();
      const threadId = threadIdRef.current;

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
              // QNT-252: bind by started_at, not first-unmatched-by-name.
              toolRows: bindToolResult(r.toolRows, ev),
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
          } else if (event === "plan_rationale") {
            const ev = data as PlanRationaleEvent;
            updateRun(id, (r) => ({ ...r, planRationale: ev.text }));
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
              narrative: annotateUnsupportedNumbers(r.narrative, unsupported),
              proseChunks: r.proseChunks.map((chunk) =>
                annotateUnsupportedNumbers(chunk, unsupported),
              ),
              // QNT-361 follow-up 3: the grounding check scores the whole
              // answer, so the structured card fields get daggers too — a
              // miss in a card summary/key point used to render unmarked
              // while the banner claimed "Numbers marked †".
              thesis: annotateUnsupportedDeep(r.thesis, unsupported),
              quickFact: annotateUnsupportedDeep(r.quickFact, unsupported),
              comparison: annotateUnsupportedDeep(r.comparison, unsupported),
              comparisonLean: annotateUnsupportedDeep(r.comparisonLean, unsupported),
              conversational: annotateUnsupportedDeep(r.conversational, unsupported),
              focused: annotateUnsupportedDeep(r.focused, unsupported),
              exploration: annotateUnsupportedDeep(r.exploration, unsupported),
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
    [ticker, updateRun],
  );

  // Cancel any in-flight stream when the panel unmounts.
  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const isStreaming = runs.some((r) => r.status === "streaming");

  // QNT-299: the context-anchor chip's source -- the most recent completed
  // turn's resolved analysis_ticker, scanning back from the latest run so a
  // still-streaming turn doesn't blank the chip before its own `done` lands.
  const contextTicker = useMemo(() => {
    for (let i = runs.length - 1; i >= 0; i--) {
      const t = runs[i].stats?.analysis_ticker;
      if (t) return t;
    }
    return null;
  }, [runs]);

  const handleContextChipClick = useCallback(() => {
    prefillComposer("Switch to ");
  }, [prefillComposer]);

  // QNT-247: announce the streamed answer of the most recent run to screen
  // readers through a debounced aria-live=polite region (frontend audit #2).
  // Only the latest run can be streaming; announcing its settled answer covers
  // both the live stream and the final text once `done` lands. The region is
  // visually hidden (sr-only) and polite, so it never moves focus — the error
  // rail keeps its own assertive role=alert path inside RunBlock.
  const latestRun = runs.length > 0 ? runs[runs.length - 1] : null;
  const liveAnswer = useDebounced(
    latestRun ? announceableAnswer(latestRun) : "",
    ANNOUNCE_DEBOUNCE_MS,
  );

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
          demo: ~20 queries/day per visitor
        </span>
      </header>

      <div ref={scrollerRef} className="min-h-0 flex-1 overflow-y-auto">
        {runs.length === 0 ? (
          <EmptyState ticker={ticker} onSuggestion={prefillComposer} />
        ) : (
          runs.map((run) => (
            <RunBlock key={run.id} run={run} onSuggestion={startRun} />
          ))
        )}
      </div>

      {/* QNT-247: visually-hidden live region carrying the streamed answer. */}
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {liveAnswer}
      </div>

      <Composer
        ticker={ticker}
        contextTicker={contextTicker}
        onContextChipClick={handleContextChipClick}
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
