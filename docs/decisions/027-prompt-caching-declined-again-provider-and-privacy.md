# ADR-027: Prompt caching on the paid synthesize call — declined again; the only implicit-caching provider is data-policy-excluded

**Date**: 2026-07-05
**Status**: Accepted
**Extends**: [ADR-026](026-paid-synthesis-economics-and-free-tier-simplification-dividend.md) (this is the QNT-318 follow-up ADR-026 filed — the "single biggest remaining cost lever" it flagged). Re-opens and closes [ADR-021](021-synthesis-model-and-tail-routing.md) #11 (the original decline on the free 8K-TPM wall). Cross-links [ADR-025](025-paid-launch-primary-and-breaker-recalibration.md) (the paid DeepSeek primary + the hot-path data-privacy boundary this decision defends).

## Context

ADR-021 #11 declined a cache-capable synthesize model on a hard **free-tier capacity wall**: every cache-capable Groq host `413`d on the 8000-TPM per-request ceiling for a 9–12K synthesis call. That decline was recorded as permanent *under the free tier* (memory `reference_groq_prompt_caching`).

ADR-025 moved the synthesize/narrate primary to paid DeepSeek V4 Flash via OpenRouter, removing that TPM wall. ADR-026 measured the paid bill as **input-dominated ~12:1** (842K input vs 72K output over 14 days) and re-classified prompt caching from "declined, permanent" to "the single biggest remaining cost lever," filing QNT-318 to investigate and enable it. The cache target is the large stable prefix on every synthesize call: the ~2,400-token `SYSTEM_PROMPT` (identical across *all* synthesize traffic) plus, within a thread, the force-injected report bundle.

This ADR records the investigation outcome. The short version: **the free-tier wall is genuinely gone, but caching is still unrealizable — now on a provider-fragmentation + data-privacy constraint, not a capacity wall.**

## Decision

**Decline enabling prompt caching. Leave `litellm_config.yaml` unpinned (no `provider` routing preference on `equity-agent/default`).** Update the ADR-021 #11 "permanent under free tier" note: it was re-attempted under paid (QNT-318) and declined for a new, independent reason.

### Evidence (measured 2026-07-05, OpenRouter, `deepseek/deepseek-v4-flash`)

**1. Only the official DeepSeek provider implicit-caches — and it is excluded by our data policy.**
OpenRouter serves this model via ~16 providers (endpoints API). Per that API, exactly one — the first-party **DeepSeek** endpoint — advertises `supports_implicit_caching: true` (cached reads ~$0.0028/M, ~98% off its $0.14/M base). Pinning to it (`provider.only: ["deepseek"]`) returns:

> `404 — No endpoints available matching your guardrail restrictions and data policy.`

That first-party endpoint **retains/trains on submitted prompts**, so our OpenRouter account's data-policy guardrail filters it out. That guardrail *is* the ADR-025/026 hot-path privacy boundary ("no primary that trains on real user queries"), deliberately kept — this is not a misconfiguration to fix.

**2. The privacy-compliant providers we actually route to do not prefix-cache our stable prefix.**
A representative substantive thesis turn (real `build_synthesis_prompt`, 9,487-token prompt, reasoning-off, `max_tokens` fixed), sent twice with a 20s cache-construction window:

| Routing | Base input $/M | `cached_tokens` (cold → warm) | Real prefix cache? |
|---|---|---|---|
| **Unpinned (production today)** | $0.09 | 0 → 0 | No — and the two calls hit *different* providers (defeats any per-provider KV cache) |
| **DeepInfra pinned** ($0.09/$0.18, privacy-OK) | $0.09 | 64 → 64 | No — a flat one-block (64-token) floor, identical cold and warm; the ~2,400-token `SYSTEM_PROMPT` is **not** cached |
| **Official DeepSeek** ($0.14/$0.28) | — | — | Yes (implicit) — but **blocked by the data-policy guardrail above** |

Unpinned, OpenRouter load-balances across providers, so consecutive turns rarely land on the same endpoint — a per-provider prefix cache can't form even where a provider supports one. Pinned to a privacy-compliant provider (DeepInfra), the warm call still shows only 64 cached tokens (one DeepSeek cache block), i.e. no meaningful prefix caching of our stable prefix.

**3. Even if the DeepSeek provider were allowed, the economics are marginal.**
Pinning to it raises base input **$0.09 → $0.14/M (+56%)**. Break-even vs unpinned needs a cache-hit fraction **> ~36%** of input tokens (solving `0.14·(1−f) + 0.0028·f = 0.09` → `f = 0.05 / 0.1372 = 0.364`). The guaranteed cross-call cacheable prefix (`SYSTEM_PROMPT`, ~2.4K) is only ~25% of a ~9.5K synthesize call — below break-even even in the ideal case. The remaining cacheable mass (the report bundle) only repeats on multi-turn *same-ticker* follow-ups within the cache TTL, a minority of the observed demo traffic (ADR-026: ~10.6 turns/day, mostly short threads). And the absolute stakes are tiny: ADR-026 measured the whole bill at **$0.19/mo** (~$1.90 at 10×).

The prompt is *already* structured for caching (`build_synthesis_prompt` documents "stable prefix + volatile suffix"; `SYSTEM_PROMPT` is static string concatenation with no per-request date/version ahead of it), so no code change would unlock a win the providers don't offer.

## Alternatives Considered

- **Pin the primary to the official DeepSeek provider to get implicit caching.** Rejected on two independent grounds: (a) it routes real user queries to a prompt-retaining/training provider, violating the ADR-025/026 hot-path privacy boundary that the data-policy guardrail enforces — a privacy regression, not a cost win; and (b) its +56% base price loses to unpinned below a ~36% hit rate we cannot reach.
- **Pin to a privacy-compliant provider (DeepInfra) for its listed `input_cache_read` discount.** Rejected: measured no real prefix caching (flat 64-token floor), and pinning forfeits OpenRouter's cross-provider load-balancing and failover resilience for ~$0.000005/turn.
- **Explicit `cache_control` breakpoints (the Anthropic-style manual path).** Not applicable: DeepSeek caching is implicit-only, and no privacy-compliant provider for this model exposes a manual-cache mechanism to target.
- **Restructure the prompt to enlarge the stable cacheable prefix** (e.g. move more into `SYSTEM_PROMPT`). Moot while no privacy-compliant provider caches the prefix at all; would be premature optimization against a capability we don't have.

## Consequences

- **The cost lever ADR-026 flagged stays closed** — but ADR-026 already established cost is not the binding constraint ($0.19/mo). Reliability and quality remain the real levers; nothing here changes them.
- **No quality dimension to test.** Prompt caching is a transparent KV optimization — served tokens and sampling are identical whether or not a prefix was cached — so the "no golden regression" acceptance criterion is moot in the decline branch (there is no output change to regress). The golden sweep already ran on this exact model in ADR-025 (QNT-258).
- **The ADR-021 #11 decline stands, with a corrected reason.** It is no longer "the free 8K-TPM wall (permanent under free tier)"; it is "no privacy-compliant OpenRouter provider implicit-caches this model, and the one that does is data-policy-excluded." Memory `reference_groq_prompt_caching` updated with the OpenRouter finding.
- **`litellm_config.yaml` is unchanged and unpinned by design.** Documented here so a future reader doesn't mistake the absence of a `provider` block for an oversight — pinning is the *rejected* option.

### Revisit-when triggers

- **Our OpenRouter data policy is deliberately relaxed** to admit a caching provider — a privacy decision (routing real queries to a training provider), owned by the user, not a silent config edit.
- **A privacy-compliant provider adds implicit caching** for `deepseek-v4-flash` (re-run the two-call measurement; if the warm call caches the ~2.4K prefix, re-price).
- **Traffic shifts to sustained multi-turn same-ticker threads** where within-thread prefix reuse dominates and the hit-rate clears ~36%.
- **The primary moves** to a provider/model with first-party, privacy-safe caching (e.g. a funded frontier tier) — revisit alongside ADR-026's alternate-primary trigger.
