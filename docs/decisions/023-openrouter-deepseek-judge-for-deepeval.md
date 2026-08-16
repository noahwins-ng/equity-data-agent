# ADR-023: Paid OpenRouter (DeepSeek V4 Flash) judge for the DeepEval RAGAS suite

**Date**: 2026-06-25
**Status**: Accepted

## Context

QNT-275 re-derives the DeepEval RAGAS floors against a measured baseline and
turns enforcement on. The design-doc calibration target is a 50-200 record
baseline (a 50-record set reliably catches a >5% regression).

The blocker is cost, not design. Each judged record fires ~12 judge calls across
the 5 metrics (claim extraction + per-claim/chunk/statement verdicts), ~25-30k
judge-tokens/record. The judge was pinned to a FREE bench model
(`bench-cerebras-gptoss120b`, no fallback by design -- a bench judge must be one
fixed model for reproducible scores). Free-tier daily token budgets
(Cerebras ~1M/day, Groq gpt-oss-120b 200K/day) cap a fixed-judge run at ~20-35
records/window, so a >=50-record baseline could not complete in one clean window
-- it walled mid-run and forced a multi-day batching workaround.

(An earlier same-session decision, never merged, rescoped the floor down to n=20
to fit the free budget. This ADR supersedes and deletes that workaround: the
constraint that motivated it is removed.)

## Decision

**Route the DeepEval judge through a paid OpenRouter model -- DeepSeek V4 Flash
(`openrouter/deepseek/deepseek-v4-flash`) -- via a dedicated LiteLLM alias
`equity-agent/bench-deepseek-v4-flash` and `DEEPEVAL_JUDGE_ALIAS`.** This:

- **Removes the daily-token ceiling** -- a full >=50-record baseline runs in one
  window for **~$0.18** ($0.089/$0.224 per M in/out). The n>=50 floor is restored;
  no batching, no rescope.
- **Improves judge quality** -- a frontier-class MoE judge (284B/13B active, 1M
  context) is a stronger RAGAS verdict model than the free gpt-oss-120b.
- **Stays reproducible** -- pinned model, `temperature: 0`, `reasoning_effort: low`
  (strips the CoT token sink; verdict tasks don't need it), structured-outputs
  supported.
- **Is scoped to the DeepEval suite only** -- `get_judge_llm(model_alias=...)`
  leaves the dialogue / golden judge on the free `JUDGE_ALIAS`. No other eval
  changes judge.

This is a deliberate, narrow exception to the project's free-provider default
(ADR-011): it is an OFF-the-hot-path eval that runs manually / on dispatch, at
pennies per run, never on the prod request path. Prod inference stays free.

## Alternatives Considered

- **Keep the free Cerebras judge + rescope to n=20.** Fits the free budget but
  ships a sub-50 baseline (weaker statistical power) and required a multi-day
  batching workaround. Rejected once paid-eval-at-pennies was on the table.
- **Free Groq gpt-oss-120b as a judge fallback.** 200K TPD -- 5x smaller than
  Cerebras -- adds only ~4 records and blends two providers into one baseline,
  eroding reproducibility. Rejected.
- **Multi-day batching on the free judge.** Works, keeps everything free, but
  spans days for an advisory signal and needs a stateful accumulator. Rejected as
  disproportionate now that a single paid run costs $0.18.

## Consequences

- AC2/AC3/AC4 are satisfiable in one clean window at the proper n>=50 floor.
- A new env var `OPENROUTER_API_KEY` is required wherever the DeepEval suite runs
  (dev + the `llm-eval.yml` job secrets). NOT needed on the prod runtime path
  (the suite is dev/CI-only), so the prod image is unaffected.
- The judged eval now has a small real-money cost (~$0.18/run). Acceptable: it is
  manual / on-dispatch, not per-PR.
- Future judged-eval baselines are no longer token-budget-bound; baseline size is
  a statistical-power choice again, not a free-tier-ceiling compromise.
- The free-tier ceiling finding is retained in agent memory
  (`reference-deepeval-tpd-ceiling`) as the rationale for why a paid judge was
  worth it.

## Amendment (QNT-442, 2026-08-16): judge moved to the 0731 GA build

`equity-agent/bench-deepseek-v4-flash` moved from the undated preview slug to
`openrouter/deepseek/deepseek-v4-flash-0731`, in lockstep with the
`equity-agent/default` primary (ADR-025 amendment) -- a judge/primary version
split would make prod behavior and DeepEval scores diverge silently.

**Re-baseline (55 records, `deepeval_history.csv`) vs the 2026-06-25
baseline (n=55, same judge model family, old build) -- every metric
improved:**

| metric | 06-25 baseline | 0731 | floor |
|---|---|---|---|
| faithfulness | 0.8309 | 0.9657 | 0.70 |
| answer_relevancy | 0.8728 | 0.8976 | 0.75 |
| context_precision | 0.7029 | 0.8727 | 0.60 |
| context_recall | 0.9667 | 0.9909 | 0.85 |
| geval | 0.78 | 0.8509 | 0.65 |

All five pass their floor with room. `number-grounding` (deterministic,
separate from the RAGAS metrics): 51/55 clean.

**Failure modes during the run, all pre-existing categories, none new**:
~5-6 of ~750+ judge calls (~1%) hit either a request timeout or the same
`Truths`-schema malformed-JSON shape QNT-258 first documented on the 0423
build. The bench alias has no fallback by design (a bench judge must be one
fixed model for reproducible scores), so these calls failed outright rather
than silently substituting a different model; `deepeval_eval.py` catches the
per-metric exception and records that one metric as NaN for that case,
which is why the run still completed cleanly at n=55. 0731 is not immune to
the failure mode QNT-258 found, but it is not more frequent either.
