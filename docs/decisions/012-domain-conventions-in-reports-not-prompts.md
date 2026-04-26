# ADR-012: Domain conventions belong in the report layer, not the prompt

**Date**: 2026-04-26
**Status**: Accepted

## Context

QNT-67's hallucination scorer (`packages/agent/src/agent/evals/hallucination.py`)
treats every numeric token in the agent's thesis as a fact-claim that must
appear verbatim in one of the supplied tool reports — that's the
operational form of ADR-003's "LLM never does math, every number from
reports" rule. It works well for company-specific facts (revenue, EPS,
P/E, RSI value) but produced a class of false positives that this ADR
addresses.

QNT-136's measurement sweeps after the QNT-133 prompt restructure surfaced
the issue concretely:

- **Run `20260426T081639Z-d136eb`**: 13/16 hallucination_ok, three records
  failed identically with `unsupported: 75` — the model was parroting the
  literal `RSI > 75` example from the new SYSTEM_PROMPT into the verdict
  action line.
- **Run `20260426T082316Z-d8d42e`** (after dropping the `75` literal from
  both the prompt body and the `Thesis.verdict_action` field description):
  14/16, but the leak class shifted to `70` and `80` — the model reaching
  for canonical RSI overbought/oversold thresholds that GOOGL's report
  text didn't print (NVDA's report does cite "above 70 threshold" and
  passed cleanly; GOOGL's report says "approaching overbought" with no
  number and the model filled in 70 from prior knowledge).

The natural reaction is to call this a hallucination — but `RSI > 70 =
overbought, < 30 = oversold` is canonical TA knowledge. It's not a
fact-claim about a specific company; it's the convention the indicator
itself is defined against. The model isn't fabricating, it's *defining
its terms*. Forbidding the convention by prompt language risks two
failure modes:

1. The model writes weaker, more qualitative action lines ("watch for RSI
   to exit the overbought bucket") that lose specificity, hurting downstream
   judge scores. Run `20260426T083425Z-f50e0f` confirmed this — judge
   collapsed from 6.5 to 2.94 with no hallucination improvement (just
   shifted leakage to a different class).
2. The instruction itself bleeds. Earlier QNT-133 SYSTEM_PROMPT contained
   the example `RSI > 75` and the model parroted that exact "75" — telling
   the model "don't write 70" risks the same dynamic.

## Decision

**Domain conventions are surfaced in the report layer (the Worker), not
the prompt (the Executive).** The technical-report template
(`packages/api/src/api/templates/technical.py`) prints the canonical
overbought/oversold thresholds *in every non-N/M RSI bucket*, not just
the boundary buckets that already cited them. Consequence: the agent's
synthesize step quotes the threshold from the supplied report rather
than reaching for prior knowledge, and the QNT-67 hallucination scorer
matches normally because the digits are in the corpus.

Concretely, `_rsi_label` now appends both thresholds to every bucket:

| RSI bucket | Old label | New label |
| -- | -- | -- |
| Overbought (≥ 70) | `overbought (above 70 threshold)` | `overbought (above 70 threshold; oversold ≤ 30)` |
| Approaching overbought (65–70) | `approaching overbought` | `approaching overbought (70 threshold; oversold ≤ 30)` |
| Neutral (35–65) | `neutral (30-70 range)` | `neutral (overbought ≥ 70, oversold ≤ 30)` |
| Approaching oversold (30–35) | `approaching oversold` | `approaching oversold (30 threshold; overbought ≥ 70)` |
| Oversold (≤ 30) | `oversold (below 30 threshold)` | `oversold (below 30 threshold; overbought ≥ 70)` |

The prompt itself only says "reference values that appear verbatim in the
reports" without enumerating any numbers — eliminating the
prompt-bleed regression class entirely. The regression test
`test_system_prompt_contains_no_multi_digit_literals` and its sibling
in `test_thesis.py` pin this invariant.

## Alternatives Considered

**A. Loosen the hallucination scorer to allowlist canonical TA thresholds.**
Cheap to implement (one set of digits in `hallucination._magnitude`),
but the line between "convention" and "fact" becomes a slippery slope —
P/E ratio thresholds (15 = cheap, 30 = rich), debt/equity ranges, ROE
thresholds, etc. Each addition is a discrete judgment call. This trades
a clean architectural boundary for a maintenance-heavy allowlist that
drifts with whoever last updated it.

**C. Loosen the prompt to permit canonical TA references without report citation.**
Adopted briefly (`Watch out for conventional-threshold leakage...`)
and rolled back when the over-tightened paragraph collapsed judge scores
to 2.94 with no improvement in hallucination_ok. Two losses for one
attempted gain. The rule "you may use general TA knowledge" muddies
ADR-003's hard "every number from reports" boundary in ways that are
hard to reverse later.

**B. (Chosen.)** Surface conventions in the report. Single source of
truth stays the report; the agent stays a strict quoter; the scorer
stays simple regex-vs-corpus; the boundary stays clean. Slight cost:
the technical-report body grows by ~30 chars per RSI line. Reports are
text consumed by the LLM, not paid-token bottlenecks, so this is
negligible.

## Consequences

**Positive**

- TA-threshold leakage class disappears empirically. The `70` / `80`
  failures observed in run `20260426T082316Z-d8d42e` (pre-template-fix)
  did not recur in either of the two post-template-fix sweeps:
  `20260426T085600Z-9433e1` (interim measurement, written to /tmp during
  iteration) and `20260426T091357Z-625e57` (canonical baseline, appended
  to the committed `evals/history.csv`). Both post-fix runs show all
  TA-only records (`*-technical`) clean and residual leakage on the
  fundamental/news side. The canonical baseline going forward is `625e57`;
  `9433e1` is referenced only as the corroborating second observation.
- Future models (Gemma, Qwen3, gpt-oss-20b, etc. — see QNT-129's bench)
  inherit the same scorer-clean behaviour without needing to know TA
  conventions. Provider-agnostic.
- ADR-003's "intelligence vs math" boundary stays intact at the prompt
  layer; no special-case carve-outs.
- The pattern generalises: when a future leak class appears around a
  domain convention (fundamental thresholds, NLP sentiment scales, etc.),
  the right fix is the corresponding template, not the prompt.

**Negative / open questions**

- The fundamental-report template (`packages/api/src/api/templates/fundamental.py`)
  carries implicit thresholds in its `_signal_verdict` (P/E < 20 bullish,
  > 40 bearish; revenue YoY > 10% bullish, < 0 bearish; net margin > 15%
  / < 0; ROE > 15% / < 0). Run `20260426T085600Z-9433e1` showed leakage
  shifting to fundamental numbers (`27.5`, `50`, `500`, `100`, `99`,
  `0.3`, `40`) once TA was clean — same root cause, different surface.
  Tracked as a separate follow-up ticket so QNT-136 can ship the TA
  half cleanly.
- Sweep variance is significant at temp=0.2 — single-run hallucination_ok
  numbers (12, 13, 14 of 16) jitter run-to-run. The post-QNT-133
  baseline is ranged, not pointwise. QNT-129's bench should report
  multi-run averages or fix temperature when comparing models.

## Rebaseline

QNT-128's baseline run `20260425T142759Z-f1aa25` (16/16 hallucination_ok,
prompt v1318d12a6d) is retired as a like-for-like comparison point.
The QNT-133 prompt explicitly demands an action line per thesis, which
both raises judge scores (richer guidance) and increases the surface
area for numeric claims. New canonical baseline is run
`20260426T091357Z-625e57` (12/16 hallucination_ok, prompt va2ff656d69,
on commit `9148052`) — the row appended to `evals/history.csv` in the
QNT-136 PR. RSI-class leakage is eliminated; residual leakage is on
fundamental-side conventions and is tracked as QNT-137 follow-up.
