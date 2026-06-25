# ADR-023: DeepEval baseline floor is n>=20 (budget-bound), not n>=50

**Date**: 2026-06-25
**Status**: Accepted

## Context

QNT-275 hardens the QNT-264 DeepEval LLM-judged RAG suite: it replaces the
shape-describing golden references (which made `context_recall` read a structural
0.29 artifact) with recall-appropriate references attributable to the gathered
reports, re-derives the five metric floors against a measured baseline, and flips
the `assert_test` enforcement gate on.

AC2/AC3 originally required the floors to be re-derived against a **>=50-record**
baseline. That number was inherited, not measured against this eval's cost:

- The design doc (`v2-overall-enhancement.md`, "RAG eval framework") sets a
  calibration target of a **50-200** record golden set ("a 50-q set reliably
  catches >5% regressions").
- The deterministic retrieval eval (QNT-261) enforces `MIN_QUERIES = 50` for the
  same statistical reason.

Both are **LLM-free** evals where a sample costs nothing, so 50-200 is trivially
affordable. The DeepEval generation eval is the opposite: each judged record runs
the live agent and ~10-12 judge calls across 5 metrics (claim extraction +
per-claim/per-chunk/per-statement verdicts + CoT reasons), costing **~48k
judge-tokens/record**. The fixed bench judge (`bench-cerebras-gptoss120b`, no
fallback by design — a bench judge must be one pinned model for reproducible
scores) has a free-tier **daily token budget of ~1M (Cerebras) / ~200K (Groq)**,
which caps a single clean window at **~20 records**. A >=50-record run needs
~2.6M tokens -- ~2.5x a full daily budget across both providers -- so it is
**budget-infeasible in a clean window**. Empirically the run walled at record ~21.

## Decision

**Rescope the QNT-275 baseline floor from n>=50 to n>=20.** Re-derive the five
`THRESHOLDS` against a >=20-record clean-window baseline (sampled from the
55-record `deepeval_recall.yaml`), then flip `DEEPEVAL_ENFORCE_THRESHOLDS` on.

n=20 is the most a fixed free-tier judge can record in one clean window, and it
still yields a real multi-record mean. The statistical cost is modest: ~20
samples reliably catches a ~8-10% regression rather than the >5% a 50-sample set
catches -- acceptable for an **off-the-per-PR-hot-path advisory gate** (it is
never a PR blocker; the per-PR RAG gate stays the deterministic retrieval +
number-grounding layer). The *fix validation itself* (context_recall 0.29 -> 1.0)
was decisive at n=2 and never depended on n=50.

The recall golden set stays at 55 records (full ticker + intent coverage and
headroom); only the recorded baseline sample is n>=20.

## Alternatives Considered

- **Hold n>=50 via multi-day batching** (~20 records/day on Cerebras x ~3 days,
  accumulated). Feasible and keeps the pinned judge, but spans days for an
  advisory signal whose fix is already proven. Rejected as disproportionate.
- **Add a Groq gpt-oss-120b judge fallback.** Groq's free tier for the same model
  is 200K TPD -- 5x smaller than Cerebras -- so it adds only ~4 records before
  walling, and a Cerebras->Groq fallback blends two providers' instances into one
  baseline, eroding the reproducibility a pinned bench judge exists to provide.
  Rejected (and the bench judge keeps its no-fallback design).
- **Switch the judge to a higher-budget model** (e.g. Groq llama-4-scout, 500K
  TPD). Different model family scores differently (QNT-129), so it would change
  the judge identity the floors calibrate against. Rejected.
- **Reduce per-record token cost** (fewer metrics / `include_reason=False`). Halves
  to ~24k/record (~40 records/window) but still short of 50 and either drops AC1's
  required RAGAS-set + G-Eval or strips the judge's explanations. Rejected.

## Consequences

- AC2/AC3 are satisfiable in one clean window; QNT-275 ships without a multi-day run.
- The enforcement gate is anchored to a real (if smaller) measured baseline,
  catching ~8-10%+ regressions -- a genuine tripwire, not the design-doc
  aspirations.
- Future LLM-judged evals inherit the lesson: **baseline size for a judged eval is
  bounded by the judge's daily token budget, not chosen for statistical power
  alone.** The deterministic-eval 50-floor does not transfer. (See
  `reference-deepeval-tpd-ceiling` in agent memory.)
- A larger baseline remains a one-line follow-up (`--sample N` + multi-day batching)
  if the advisory signal ever needs >5% sensitivity.
