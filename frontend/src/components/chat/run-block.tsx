// ─── Run renderer (one user prompt → one streamed answer) ────────────────
//
// QNT-247: memoized so a streamed token only reconciles the ONE streaming run.
// `updateRun` replaces just the updated run in the runs array (every other run
// keeps its object identity), and `onSuggestion` is the stable `startRun`
// useCallback — so React.memo's shallow prop compare lets all prior runs in the
// tape (up to MAX_RUNS) skip re-render while narrative_chunk / prose_chunk
// deltas stream into the live run.

import { memo } from "react";

import { hasAnswerSurface, isComposing, showCardProse } from "../chat-run";
import { ComparisonCard } from "./comparison-card";
import { ComposingBubble } from "./composing-bubble";
import { ConversationalCard } from "./conversational-card";
import { ExplorationCard } from "./exploration-card";
import { FocusedAnalysisCard } from "./focused-analysis-card";
import { LeanComparisonCard } from "./lean-comparison-card";
import { NarrativeBubble } from "./narrative-bubble";
import { ProseBlock } from "./prose-block";
import { QuickFactCard } from "./quick-fact-card";
import { RetrievedSources } from "./retrieved-sources";
import { ThesisCard } from "./thesis-card";
import { ToolCallRow } from "./tool-call-row";
import type { ChatRun } from "./types";

export const RunBlock = memo(function RunBlock({
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
});
