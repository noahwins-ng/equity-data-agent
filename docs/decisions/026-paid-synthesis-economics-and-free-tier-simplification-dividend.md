# ADR-026: Paid synthesis economics + the free-tier simplification dividend

**Date**: 2026-07-05
**Status**: Accepted
**Extends**: [ADR-025](025-paid-launch-primary-and-breaker-recalibration.md) (QNT-258 - the forced paid-primary wiring). This ADR does **not** re-decide the provider or re-wire the alias; it records the two deliverables ADR-025 deferred: (1) the **economics** ADR-011 was missing, and (2) the **simplification dividend** - which defensive layers, built ticket-by-ticket around free-tier walls, can now be deleted because synthesis is paid. Cross-links [ADR-011](011-llm-routing-groq-default-gemini-override.md) (free-to-clone constraint), [ADR-021](021-synthesis-model-and-tail-routing.md) (cache decline + tail routing), [ADR-023](023-openrouter-deepseek-judge-for-deepeval.md) (paid-OpenRouter judge precedent).

## Context

A striking share of the agent architecture is downstream of Groq's free-tier walls, accreted one ticket at a time: the transitive fallback chain + served-model tracker (QNT-227 / QNT-230), the proxy-timeout tail choreography (ADR-021 #12), per-node small-model tiering (QNT-220 #7), the declined prompt-caching lever (ADR-021 #11), per-request token-budget accounting (QNT-161), and variance-aware eval gating (QNT-218).

This started as an accept/decline spike: *should* `synthesize`/`narrate` move to a paid alias, or does ADR-011's free-to-clone constraint hold? The 2026-06-17 Groq decommission removed the "decline" branch - after 2026-08-16 Groq has **no** free model that can serve a 9-12K synthesize within its per-request TPM ceiling (ADR-025 §Context). Paid (or non-Groq) synthesis is forced, and QNT-258 shipped that wiring under deadline (DeepSeek V4 Flash via OpenRouter, paid). ADR-023 already broke the free-to-clone seal for the eval judge; ADR-025 broke it for the primary. This ADR records the **payoff** of that second, larger break.

## Decision

Two records, no new wiring.

### 1. Economics - the number ADR-011 was missing

`synthesize`/`narrate` are the only two nodes on the paid `equity-agent/default` alias; `classify`/`plan` stay on the free `equity-agent/small` (gpt-oss-20b, survives the decommission - QNT-220 #7), so **the free tier still carries the classify/plan path and only synthesis is billed.**

**Token evidence** - Langfuse, 14-day window **2026-06-21 through 2026-07-04** (query bound `2026-06-21T00:00Z → 2026-07-05T00:00Z`, exclusive end = 14×24h), trace `agent-chat`, **148 turns**. Generations grouped by served alias (the `default`-destined heavy calls - including the calls that overflowed to the Groq Scout fallback under the *pre*-258 regime - are the synthesis path; `small` is excluded as it stays free):

| Path (alias) | gens | input tok | output tok |
|---|---|---|---|
| `equity-agent/default` (synthesize/narrate) | 161 | 423,240 | 32,664 |
| Scout fallback overflow (synthesis, throttle) | 65 | 415,836 | 39,123 |
| gpt-oss-120b terminal (synthesis) | 1 | 3,647 | 61 |
| **Paid synthesis path - total** | **227** | **842,723** | **71,848** |
| `equity-agent/small` (classify/plan - *stays free*) | 149 | 197,003 | 13,564 |

**Paid tokens/turn: ~6.2K** (5,694 in / 485 out, averaged over all 148 turns - input-dominated ~12:1). A *substantive* thesis turn is heavier (~12K in / ~1.5K out ≈ the 9-14K synthesis regime of ADR-021/025); the 6.2K average is diluted by lightweight conversational/quick-fact turns, and that mix is exactly the right basis for projecting real traffic cost.

**Projected USD/month** (30-day, at observed demo traffic ≈ 10.6 turns/day → ~317 turns/mo, and at 10×). Prices are list $/M in / out:

| Provider (per-M in/out) | $/turn | 1× (~317/mo) | 10× (~3,170/mo) |
|---|---|---|---|
| **DeepSeek V4 Flash** - OpenRouter, chosen ($0.09 / $0.18) | $0.0006 | **$0.19** | **$1.90** |
| GPT-4o-mini - alternate ($0.15 / $0.60) | $0.0011 | $0.36 | $3.63 |
| Groq llama-3.3-70b on-demand paid - the ADR-021 incumbent ($0.59 / $0.79) | $0.0037 | $1.19 | $11.87 |

Notes: DeepSeek's headline $0.09/$0.18 is the ADR-025 launch figure; ADR-023 measured $0.089/$0.224 for the same model - the cent-level output delta moves the monthly total by <$0.01 at 1×, immaterial to the decision. The Groq-70b row is the "~6× more expensive than DeepSeek" comparator from ADR-025 and is **illustrative only** - that model is decommissioned 2026-08-16, which is *why* it isn't the primary. **Reference stress scenario** (ADR-025's launch-night envelope): 500 substantive thesis chats/day at ~$0.00135/turn on DeepSeek ≈ **$20/mo (~$0.68/day)** - consistent with ADR-025's "~$1/day worst case." Cost is not the constraint at any of these scales; reliability is.

**Honest caveat on the window:** it straddles the config change (QNT-258 shipped 2026-07-04), so most turns ran on the *old* Groq primary. That does not distort the projection: per-node token **sizes** are provider-independent (a synthesize prompt is the same size whichever model serves it), and DeepSeek input/output pricing is what's applied. The window is a pre-public-launch demo, hence the small 1× - the 10× and launch-burst rows bound the public scenario.

### 2. Simplification dividend - keep / remove / simplify

Each defensive layer built for a free-tier wall, with a verdict now that synthesis is paid:

| Layer | Built for | Verdict | Rationale |
|---|---|---|---|
| **Transitive fallback chain** (QNT-227/230; deep free Groq hops) | Groq TPD exhaustion on the hot path | **REMOVE (residual)** | ADR-025 already collapsed `default → nemotron-ultra → deterministic`. What remains is dead: the `small` chain still routes `small → default → fallback-llama4scout → gpt-oss-120b`, and Scout (07/17) + the `bench-llama4scout`/`bench-qwen3-32b`/`bench-llama3-70b` aliases (07/17-08/16) point at retiring models. Paid primary + the decommission make this deletable. → **follow-up ticket, deadline-driven.** |
| **Served-model tracker** (QNT-230; x-litellm-* served-model in trace metadata) | Knowing which fallback actually served, for eval integrity | **KEEP** | Provider-independent bench-integrity instrumentation; cheap, useful precisely because fallback still exists (just shallower). Not a free-tier artifact. |
| **Proxy-timeout tail choreography** (ADR-021 #12; per-model `timeout: 45` → in-call fallback) | Bounding a throttled ~120s Groq tail | **KEEP (rationale changes)** | DeepSeek's p90 is 37s vs llama's 128s (ADR-025) - the 120s retry-stack that motivated it is gone, but the timeout is a cheap generic slow-primary guard. Retune optional; no deletion. |
| **Per-request token-budget accounting** (QNT-161; per-IP 30K/day + global breaker) | Rationing free TPD across public users | **KEEP** | The per-IP abuse fence stays regardless (cost/abuse, not free-tier). The global breaker was already re-purposed 200K→20M by ADR-025 (from "free tokens left" to "runaway-cost guard"). A dollar-cap replacement is a noted-not-adopted refinement (ADR-025). No deletion. |
| **Variance-aware eval gating** (QNT-218) | Partly Groq MoE serving non-determinism | **KEEP** | Any LLM eval has run-to-run variance; variance-aware gating is sound statistical practice regardless of provider. The Groq-MoE-jitter driver is reduced (temperature-0 DeepSeek), not the whole rationale. No deletion. |
| **Prompt caching** (ADR-021 #11; declined) | Declined on the free 8K-TPM per-request wall | **REVISIT → ENABLED** ([ADR-027](027-prompt-caching-enabled-via-provider-pin.md)) | Paid removes the 8K-TPM wall. QNT-318 found the real blocker was OpenRouter load-balancing across ~16 per-provider caches (unpinned `cached_tokens=0` - calls hop providers). Fixed with an ordered provider pin on `equity-agent/default` (sticky to Novita → ~100% warm prefix cache, verified through litellm). Privacy-compliant providers lead the order; a per-alias `data_collection: allow` keeps the curated top-6 available without touching the account default. → **enabled, config change shipped.** |
| **Per-node small-model tiering** (QNT-220 #7; classify/plan on free `small`, synthesize/narrate on paid `default`) | 70b tokens scarce on free Groq TPD | **KEEP** | gpt-oss-20b survives the decommission, so the tier stands (ticket out-of-scope: "tiering stands"). Keeping classify/plan on the free `small` alias saves cost *regardless* of synthesis billing - moving them onto the paid primary would add tokens for no quality gain. Not a free-tier-wall artifact to unwind; the split is a permanent efficiency choice. No deletion. |

Two "removable/revisit" items are filed as follow-ups referencing this ADR:

- **QNT-317** - `fix(infra)`: re-anchor + retire the Groq free-tier fallback chain before the decommission (delete the dead Scout/70b hops from the `small` chain + retire the `bench-llama4scout`/`bench-qwen3-32b`/`bench-llama3-70b` aliases). Deadline: Scout dies 2026-07-17.
- **QNT-318** - `feat(infra)`: revisit prompt caching on the paid synthesize call now the 8K-TPM wall is gone (input-token-dominant; the biggest remaining cost lever).

Everything else stays: the keep-column layers are provider-independent safety/observability, not free-tier scaffolding.

### 3. Revisit-when triggers + the free-model boundary

Reopen this ADR if any fire:

- **OpenRouter DeepSeek pricing drifts materially** (say >3× the $0.09/$0.18 assumed here), or the model is deprecated - re-run the token×price table and re-pick.
- **Traffic sustains >10× the observed demo** (>~100 turns/day for a month) - the 10× row becomes 1×; recheck whether the global breaker (20M/day) and per-IP fences still bound cost as intended.
- **Prompt caching is now enabled** - QNT-318 ([ADR-027](027-prompt-caching-enabled-via-provider-pin.md)) shipped an ordered provider pin so routing sticks to a caching provider (Novita, ~100% warm prefix). The $/turn and "input-dominated" framing here should be restated against cached-prefix pricing once prod Langfuse shows the steady-state hit rate.
- **A funded/monetised path opens** a frontier primary (Claude/GPT-5 class) as a quality tier - LiteLLM makes it a YAML edit; revisit the alternate table.

**Free-model boundary (unchanged from ADR-025).** Free models (OpenRouter `:free`, OpenCode Zen, etc.) are **bench-harness-only** - shared RPM/RPD caps, ephemeral capacity, and they train on submitted prompts (real user queries). The one deliberate exception is the single free **fallback anchor** (Nemotron 3 Ultra), which carries only the rare residual traffic the paid primary drops (ADR-025 §2). The primary stays paid; no free model serves the hot path.

## Alternatives Considered

- **Fold the economics into ADR-025 and skip a new ADR.** Rejected: ADR-025 shipped under the 07/17→08/16 deadline and deliberately scoped to the survival move; the cost table and the dividend are a distinct, non-urgent analysis. A separate ADR keeps the survival decision and its follow-on cleanup separately reviewable (the same rationale that keeps each deletion on its own follow-up ticket).
- **Execute the deletions inside this ticket** instead of filing follow-ups. Rejected: the ADR's job is to *decide and enumerate*; each removal (fallback re-anchoring, prompt caching) touches live config/behavior and deserves its own reviewable change with its own verification. Bundling them into a docs ticket would hide runtime changes behind an ADR merge.
- **Dollar-cap the global breaker now** (vs. the token-cap ADR-025 recalibrated). Deferred, not adopted - it needs per-token pricing + dollar accounting in `TokenBudget`; the token cap already fails closed and the comment expresses the derivation in dollars. Listed as a KEEP-with-optional-refinement, not a removal.

## Consequences

- **The free-to-clone story is now precisely bounded, with a number.** A cloner running the default provider needs an `OPENROUTER_API_KEY` and pays ~$0.19/mo at demo traffic (~$2/mo at 10×); the free `equity-agent/gemini` override (ADR-011) remains a one-line zero-cost switch, and the fallback anchor is free. "Free to clone" became "≈$0.19/mo to clone, or free on the gemini override."
- **The dividend is real but modest in LoC and gated behind two follow-ups.** Most of the free-tier scaffolding (per-IP fences, the tail timeout, variance gating, served-model tracking) is provider-independent and *stays*; the genuine deletions are the dead Groq hops (forced anyway by the decommission) and the newly-unlocked cache lever.
- **Prompt caching moves from "declined, closed" (ADR-021) to "the top open cost lever."** The same 8K-TPM wall that killed it is the wall paid inference removes; because the bill is ~12:1 input-heavy, caching the stable report/prompt prefix is where future cost work should go - recorded so the ADR-021 "closed with hard evidence" note isn't mistaken for permanence.
- **Cost is de-risked as a launch concern.** At every modelled scale - demo, 10×, and the 500-chat/day launch burst - the monthly bill is single-digit-to-low-tens of dollars. The binding constraints going forward are reliability and quality, not spend.
