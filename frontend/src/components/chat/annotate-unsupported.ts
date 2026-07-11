// ─── Grounding-miss annotation ──────────────────────────────────────────────
//
// QNT-361 follow-up (redaction UX): a number the runtime grounding check
// could not find in the supplied reports used to be REPLACED with
// "[unsupported number]" — which read terribly when the narrator had merely
// rounded a report value (real MU turn: report "45.4% discount", narrator
// "45% discount", card showed "a [unsupported number]% discount"). A reader
// could not tell "rounded" from "invented".
//
// The number now stays visible with a dagger footnote appended ("45%†");
// the amber grounding banner (run-block.tsx) explains the marker. Detection
// is unchanged — the checker still flags every unsupported token and the
// banner still shows — only the presentation softened from redact to
// annotate.

/** Append a dagger to every unsupported numeric token in ``text``.
 *
 * ``unsupported`` carries canonicalised tokens from the grounding check
 * (bare mantissas: "45", "129.2"). The token in prose may glue a trailing
 * percent or magnitude unit ("45%", "$129.2B"); those are swallowed into
 * the match so the dagger lands after the full spoken token, not inside it.
 * Longest-first ordering stops a short token ("5") from splitting a longer
 * one ("45") it is a suffix of.
 */
export function annotateUnsupportedNumbers(
  text: string,
  unsupported: readonly string[] = [],
): string {
  let annotated = text;
  for (const raw of unsupported.filter(Boolean).sort((a, b) => b.length - a.length)) {
    const escaped = raw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const pattern = new RegExp(
      `(^|[^\\d.])(${escaped}(?:%|bn|tn|mn|[KMBTkmbt])?)(?=$|[^\\d.])`,
      "g",
    );
    annotated = annotated.replace(pattern, "$1$2†");
  }
  return annotated;
}
