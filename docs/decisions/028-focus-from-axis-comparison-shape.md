# ADR-028: Focus-from-axis comparison shape and per-shape output budget

**Date**: 2026-07-10
**Status**: Accepted

## Context

Comparison is the largest output shape in the system and, until QNT-358, could
not be narrowed by the user. An axis-named comparison ("compare TSLA vs AMD on
technical momentum") fail-closed to the deterministic fallback (composite ~2,
structure axis 0, `LengthFinishReasonError` at completion_tokens=1500). Two
compounding causes:

1. **The plan never narrowed for comparison.** `plan_node` fell through to
   `plan = list(available)` for the comparison intent. The in-code justification
   ("narrowing can starve one side of the contrast") was a concern about
   ASYMMETRIC (per-ticker LLM) narrowing -- it does not apply to a deterministic
   narrow applied identically to both tickers. So an axis-named comparison
   gathered all four reports for BOTH tickers. The goldens could not catch this:
   `tool_call_ok` is subset-based by design (over-fetching passes; only a
   missing expected tool fails), so `tsla-amd-technical` pinning
   `expected_tools: [technical]` proved nothing about narrowing.
2. **The output schema forced the full matrix.** `ComparisonSection` declared
   all four `AspectView` fields as REQUIRED, so every comparison emitted eight
   AspectViews plus the differences paragraph -- structurally ~2x a single-ticker
   thesis. Combined with the QNT-351 `max_tokens=1500` cap (calibrated on the
   SINGLE-ticker thesis distribution: median ~920, p-high ~1400), the full matrix
   overshot the cap and fail-closed.

The QNT-351 cap comment claimed it "sits just above the normal synthesize
ceiling" -- true for the thesis distribution it was derived from, but comparison
was never in that distribution. That thesis-only calibration is the root
mis-fit this ADR corrects.

## Decision

Give comparison the SAME three focus axes the single-ticker focused path already
has (fundamental / technical / news; `FocusKind`), with `company` riding along
as always-included qualitative grounding. This is a symmetry, not a new
four-way capability. No new `focused_comparison` intent: the taxonomy stays
flat. Focus is derived from the question axis at plan time and from the gathered
set at render time.

- **Plan-time narrow (QNT-358 A).** A new deterministic `comparison_axis()`
  detector (a comparison-tuned keyword set, distinct from the conservative
  single-ticker focused-intent triggers) resolves the single named axis. When
  one axis is named, `plan_node` narrows the comparison plan to
  `["company", <axis>]`, applied symmetrically to both tickers -- gather runs the
  identical plan against each. Zero or more-than-one axis keeps the full
  four-aspect plan. The `plan_node` starve-one-side comment was corrected.
- **Render what was gathered (QNT-358 B).** The non-company `AspectView` fields
  on `ComparisonSection` are now optional (default None); the comparison prompt
  carries the thesis's "leave a non-supplied aspect out" rule. An aspect whose
  report was not gathered is omitted (null), and `to_markdown` (the QNT-324
  followup/narrate grounding substrate) and the frontend card both skip None
  aspects. Cause A alone was insufficient: with the plan un-narrowed, all four
  reports are gathered and the render-what's-gathered rule never triggers.
- **Strict-schema compatibility (QNT-358 C).** QNT-351 pinned strict
  `json_schema`, under which optional Pydantic fields compile to
  required-but-nullable (`anyOf` with null). The ticket flagged this as a risk to
  verify before building, with `method="function_calling"` as the fallback. It
  was verified LIVE against the pinned DeepSeek/OpenRouter provider (QNT-358 AC5):
  strict `json_schema` with the nullable AspectView fields WORKS and is
  consistent -- the provider fills exactly the supplied aspects on both tickers
  across repeated runs. The `function_calling` fallback was tested and FAILED THE
  OTHER WAY: it dropped the nullable axis aspect (rendering company only) or
  returned incomplete tool-call args that failed to coerce (-> deterministic
  fallback). So the comparison call keeps the DEFAULT strict `json_schema` method;
  `function_calling` was rejected by measurement, reversing the ticket's assumed
  direction.
- **Two-ticker output budget (QNT-358 D).** The comparison synthesize call runs
  with a two-ticker output budget (`_COMPARISON_MAX_TOKENS = 3000`), passed
  per-call via `get_llm(max_tokens=...)`. Two implementation facts, both settled
  by live measurement:
  - The override must travel as the literal `max_tokens` key in the request body
    (via `extra_body`), NOT the ChatOpenAI `max_tokens=` field: recent
    `langchain_openai` serialises that field as `max_completion_tokens`, a
    different key from the config's `max_tokens: 1500`, so both were sent and the
    provider still truncated at 1500. Sending literal `max_tokens` overrides the
    same key the config pins (verified against the proxy).
  - AC4 scoped the budget to the no-axis full matrix, on the premise that a
    narrowed two-aspect comparison is thesis-sized and fits 1500. Measurement
    falsified that premise: a narrowed `NVDA vs AAPL` fundamentals comparison
    truncated at `completion_tokens=1500` and fell to the fallback, because the
    2-ticker overhead alone lifts even a two-aspect comparison past the
    thesis-calibrated ceiling. So the budget applies to the WHOLE comparison
    path, sized for the largest (full-matrix) shape. `max_tokens` is a ceiling,
    not a target -- a narrowed comparison bills only its smaller actual output --
    so one budget for both shapes adds no cost and removes a truncation cliff.
    The QNT-351 deterministic fallback still catches a genuine runaway past this
    ceiling.

## Alternatives Considered

- **A dedicated `equity-agent/comparison` LiteLLM alias with a higher cap.**
  Rejected: it would duplicate the elaborate QNT-351 provider-pin / reasoning-off
  / timeout / fallback config, creating exactly the non-declarative drift risk
  that config block already warns about. A per-call `max_tokens` override changes
  only the one param that differs.
- **`method="function_calling"` for the comparison call** (the ticket's
  sanctioned fallback for the strict-nullable risk). Rejected by live
  measurement: `function_calling` dropped the nullable axis aspect or returned
  incomplete args, while strict `json_schema` filled the supplied aspects
  reliably. The verification the ticket asked for was run against the live proxy
  and came out the opposite way, so `json_schema` (the default) is kept.
- **Per-shape budget: default cap for narrowed, override only for full matrix.**
  Rejected by measurement: a narrowed two-aspect comparison also truncated at
  1500 on a verbose pair. One two-ticker budget for the whole comparison path is
  simpler and correct.
- **Trim folded report size instead of raising the budget.** Rejected and
  measured during QNT-353: a compact technical report at ~1/3 the size did NOT
  recover `tsla-amd`, so folded-report size is not the lever.
- **A new `focused_comparison` intent.** Rejected: the taxonomy stays flat;
  deriving focus from the question axis + gathered set needs no new intent.

## Consequences

- An axis-named comparison now renders a compact company + one-axis card for both
  tickers, thesis-sized, well inside the 1500 cap -- no `LengthFinishReasonError`.
- A no-axis "compare NVDA vs AMD" still renders the full four-aspect matrix, now
  with a two-ticker budget so it no longer fail-closes.
- The QNT-351 deterministic fallback stays the correct safety net for a genuine
  runaway; it is simply no longer the normal comparison outcome.
- The frontend comparison card and `ComparisonSection` TS type are now
  null-tolerant; a followup that reasons over a prior narrowed comparison reads a
  `to_markdown` substrate that omits the un-gathered aspects. The API's
  done-event citation counter (`_count_comparison_citations`) was the last
  consumer that dereferenced every aspect -- it now skips None. This surfaced
  only in the live in-browser check: the card and narration stream first, so the
  crash truncated the SSE tail (`ERR_INCOMPLETE_CHUNKED_ENCODING`) after a
  visually complete render -- a reminder that a nullable-shape change must sweep
  every consumer of the model, not just the renderer.
- Re-derive `_COMPARISON_MAX_TOKENS` from live Langfuse comparison
  generations once prod traffic accrues; 3000 is the structural interim value.
