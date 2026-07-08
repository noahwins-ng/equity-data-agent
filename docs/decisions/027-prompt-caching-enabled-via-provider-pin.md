# ADR-027: Prompt caching enabled on the paid synthesize call via an ordered OpenRouter provider pin

**Date**: 2026-07-05 (amended 2026-07-08, QNT-351)
**Status**: Accepted — **provider order and privacy scope amended by QNT-351 (see the amendment at the end)**
**Extends**: [ADR-026](026-paid-synthesis-economics-and-free-tier-simplification-dividend.md) (the QNT-318 follow-up it filed — "the single biggest remaining cost lever"). Closes [ADR-021](021-synthesis-model-and-tail-routing.md) #11 (declined on the free 8K-TPM wall). Relaxes, for one alias only, the hot-path data-privacy boundary of [ADR-025](025-paid-launch-primary-and-breaker-recalibration.md).

> **Correction (same-day).** The first version of this ADR *declined* caching, concluding "no privacy-compliant OpenRouter provider prefix-caches `deepseek-v4-flash`." That was wrong — it rested on an under-powered two-call cold test that stopped **before the provider's cache warmed**. The OpenRouter dashboard (aggregate hit rates: Novita 83.5%, DeepInfra 54.5%, …) prompted a re-measurement that reversed the finding. This is the corrected record; the decision is now **enable**. The methodological lesson (don't conclude from a cold, too-short cache probe) is the real takeaway.

## Context

ADR-021 #11 declined a cache-capable synthesize model on the free-tier 8000-TPM per-request wall. ADR-025 moved synthesize/narrate to paid DeepSeek V4 Flash via OpenRouter, removing that wall; ADR-026 measured the bill as **input-dominated ~12:1** and flagged caching the large stable prefix (~2,404-token `SYSTEM_PROMPT` + force-injected reports) as the top open cost lever, filing QNT-318.

The blocker turned out **not** to be "does the model cache" — it does — but **provider fragmentation**: OpenRouter serves `deepseek-v4-flash` from ~16 providers, **each with its own KV cache**, and by default load-balances across them. So consecutive synthesize calls hit different providers and never reuse a warm prefix cache. Measured unpinned: `cached_tokens=0`, with back-to-back calls landing on different providers.

## Decision

**Pin an ordered OpenRouter provider preference on `equity-agent/default` so routing is sticky to one caching provider, and allow data collection on that alias only so the curated set is fully available.** Config (`litellm_config.yaml`, under `litellm_params.extra_body`):

```yaml
provider:
  order: [novita, deepinfra, gmicloud, deepseek, alibaba, baidu]
  allow_fallbacks: false
  data_collection: allow
```

- **Ordered, not an unordered allowlist.** `order` makes OpenRouter prefer the first *available* provider, so traffic sticks to it and its prefix cache stays warm. An unordered `only:` set would let OpenRouter load-balance and re-fragment the cache. `allow_fallbacks: false` keeps routing inside the curated top-6 (by OpenRouter token share); if all six are down the litellm-level fallback to the Nemotron anchor still fires (unchanged).
- **Privacy-compliant providers first.** Novita → DeepInfra → GMICloud lead; the three data-retaining providers (DeepSeek / Alibaba / Baidu) sit last as deep resilience only. In practice traffic caches on Novita and a training provider is reached only if all three western providers are simultaneously unavailable — so the privacy exposure is essentially theoretical while the full six remain for availability.
- **`data_collection: allow` is scoped to this alias**, not the account. The OpenRouter account default stays strict (deny), so the DeepEval judge (ADR-023), the fallback anchor, and every other alias are unaffected. The relaxation is a deliberate user decision (QNT-318): the public-equity chat carries no PII and is already fully public/no-auth ([ADR-017](017-public-chat-truly-public-no-auth.md)), so a provider training on "give me a thesis for NVDA" is negligible exposure — and it only bites the rarely-used tail of the fallback order anyway.

### Evidence (measured 2026-07-05)

Representative substantive thesis turn (real `build_synthesis_prompt`, reasoning-off), same prompt repeated to warm the cache:

| Routing | Sticky? | `cached_tokens` progression | Verdict |
|---|---|---|---|
| **Unpinned (old prod)** | no — hops providers | 0 → 0 | no cache reuse |
| **Ordered top-6 pin, direct** | yes → Novita, all 5 calls | 100% warm (9,472 / 9,487) | full prefix cached |
| **Same pin, through litellm SDK** | yes → Novita | 2,432 → 8,192 → 8,192 | litellm forwards `provider`; cache engages |

Two things the litellm run proves: (1) `drop_params: true` does **not** strip the `provider` block — routing went to the ordered #1 (Novita), so the proxy will honor it in prod; (2) even call-1 of a *fresh* prompt returned **2,432 cached** — the shared `SYSTEM_PROMPT` prefix, warm from earlier traffic. That 2,432 is the floor every synthesize call gets once the system prompt is warm on Novita (~25–30% of a cold-ticker first turn); thread follow-ups on the same ticker reach ~100%.

Cache reads bill roughly **0.2× input** on Novita/DeepInfra (≈80% off the cached portion; the official DeepSeek endpoint is ~0.02× but is a data-retaining provider we keep last). Because the bill is ~12:1 input-heavy, discounting the cached input fraction is the largest available cost reduction on the synthesis path — though in absolute terms the whole bill remains small (ADR-026: ~$0.19/mo at demo traffic, single-digit to low-tens of dollars at launch scale). The change is a strict improvement: caching **plus** provider-cost control **plus** deterministic serving (reproducible evals), at the cost of concentrating on one provider (mitigated by the ordered fallback set).

## Alternatives Considered

- **Leave it unpinned (the original "decline").** Rejected on corrected evidence — unpinned demonstrably gets zero cache reuse because of provider hopping, and forgoes both the cache discount and a cheaper, deterministic provider.
- **Unordered `only:` allowlist of the six.** Rejected: without ordering, OpenRouter load-balances within the set and re-fragments the cache across six independent caches — the caching goal needs stickiness.
- **Relax the account-level data policy** to admit the official DeepSeek endpoint (deepest cache discount, ~98% off). Rejected: broad blast radius (changes routing eligibility for the judge and every alias) for a rounding-error saving over Novita, and Novita already caches ~100% warm. The per-alias `data_collection: allow` is the surgical form.
- **Pin a single provider (`only: [novita]`).** Rejected: one provider's outage/rate-limit (a 429 was observed on DeepInfra during testing) would drop the whole primary to the Nemotron anchor; the ordered six-set keeps in-family resilience first.

## Consequences

- **Caching is realized on the hot path** — the ADR-026 "top cost lever" is now pulled. The economics there should be restated against cached-prefix pricing once prod Langfuse shows the real steady-state hit rate.
- **Serving is now deterministic** (sticky to Novita), which also stabilizes eval reproducibility — a side benefit the unpinned config lacked.
- **The ADR-021 #11 decline is fully closed** — not "permanent under the free tier," and not "unrealizable under paid" (the earlier draft of this ADR); it is now *enabled*. Memory `reference_groq_prompt_caching` updated.
- **A narrow, documented privacy relaxation exists** on `equity-agent/default` only. If the public chat ever carries sensitive input, revisit: reorder to privacy-compliant-only or drop `data_collection: allow` (Novita-first still caches without it).
- **Watch**: provider concentration. If Novita degrades, latency/availability shifts to DeepInfra/GMICloud (cold cache on the switch). Revisit the order if the sticky #1 proves unreliable in prod. Also confirm the real steady-state `cached_tokens` in prod Langfuse — the measurements here are dev-side probes.
- **Watch (deep-fallback parity)** — *resolved, with a caveat (QNT-319, 2026-07-05)*. `scripts/smoke_openrouter_providers.py` pins each of the six providers individually (`provider.only`) through the litellm proxy and, reasoning-off, exercises three arms: **`synthesize(Thesis)`** — the *real* production structured shape on this alias (`synthesize` calls `_structured_call(Thesis, ...)` with no alias override, so it routes here) — plus the array-bounded `ThesisPlan` capability probe and a narrate free-text request. **All six — including the three previously-unverified deep-fallbacks (DeepSeek/Alibaba/Baidu) — can serve every arm** (`finish_reason=stop`, valid parse; a clean sweep shows 3/3 per arm on all six). DeepSeek's *direct* endpoint 404s under the account guardrail on a raw OpenRouter call, but through the proxy (the prod path) it serves cleanly, so it is not a gap.

  The caveat is a **per-attempt prose-flake** on the reasoning-off structured path — a provider intermittently returns prose instead of the JSON envelope (the QNT-258 mode). It is *intermittent*: some N=3 sweeps come back all-clean, others surface a stray flake on one or two providers (e.g. a run showed GMICloud 1/3, DeepSeek 2/3, Baidu 2/3 clean on the `ThesisPlan` arm), and a focused 6-attempt probe put DeepInfra worst (~50%) on the weak `ThesisPlan` prompt while Novita stayed 6/6; even the JSON-demanding `Thesis` prompt flaked ~1-in-4 on a small sample. This is the known QNT-196 ~5.5% parse-failure phenomenon, **bounded in prod** by `_structured_call`'s two-attempt retry + deterministic fail-close. The smoke therefore runs N attempts per arm and reports the k/N clean ratio, treating a provider as capable if it parses at least once. **No reorder** is warranted: no provider is categorically incapable, the array-bounded `ThesisPlan` never runs on these providers in prod anyway (it runs on the Groq `equity-agent/small` alias), and the deep-fallbacks are reached only if Novita is down. If Novita reliability ever degrades, prefer GMICloud/Alibaba/Baidu over DeepInfra for the #2 slot. Reproduce with `make dev-litellm` + `uv run python scripts/smoke_openrouter_providers.py` (add `--attempts N` to characterize the flake); re-run after any provider-set change.

## Amendment (QNT-351, 2026-07-08): DeepSeek-first pin, account-level privacy, strict json_schema

The QNT-258 flip to this alias quadrupled per-call latency (Langfuse: p50 1.1s→4.3s, p90 6.1s→13.1s). The 2026-07-08 investigation decomposed the gap (slow decode, verbosity growth, tripled TTFT) and found **the ADR-027 caching premise had not materialized in prod**: over 07-04..08, **27 of 33** structured calls showed `input_cache_read = 0`; the only hits were back-to-back bursts. Organic turns arrive minutes-to-hours apart and the provider KV cache evicts first.

**The premise was wrong, not just under-realized.** OpenRouter endpoint metadata (`/api/v1/models/deepseek/deepseek-v4-flash/endpoints`, `supports_implicit_caching`) reports implicit caching **FALSE for Novita and every provider in the old pin except first-party DeepSeek (TRUE)**. Novita never implicit-cached; the "Novita 100% warm" dev measurement in the Evidence table above was a back-to-back burst on a provider whose cache does not persist across organic gaps. The dashboard hit rates that drove the same-day correction were aggregate/back-to-back artifacts, not steady-state.

**Decision (amended):**

1. **First-party DeepSeek leads the pin.** It is the only implicit-caching endpoint, with near-top decode (p50 71 tok/s), best uptime (99.90%), and cache reads billed at `0.0028/M ≈ 0.02× input` (vs the ~0.2× on Novita that never engaged). Measured live: back-to-back DeepSeek calls warm **~84–95% of the prefix**, dropping call-2 cost **~14×** (`only:[deepseek]` probe: call-1 `cached=0` cost `$0.000227` → call-2 `cached=1536/1618` cost `$0.0000161`; proxy synthesize smoke: call-2 `cached_tokens=4096/4893`).
2. **Remaining order re-derived from live endpoint stats** (`throughput_last_30m` p50 / uptime / input price / quant), as pure resilience reached only if DeepSeek is down: `deepseek → baidu (p50 70, cheapest reachable input $0.098/M) → alibaba (60) → novita (53) → gmicloud (22, 90.86% uptime — kept but deep) → deepinfra (17, fp4: slowest + quantized, moved LAST)`.
3. **`require_parameters: true`** added to the provider preferences so OpenRouter routes only to endpoints that honor the request's `response_format`/`json_schema` (all 16 advertise `structured_outputs`, but QNT-258 proved advertise ≠ enforce).
4. **`max_tokens: 1500`** on the alias caps synthesize output verbosity (payload median grew ~410→920 tokens post-flip, range 390–2430; 1500 sits above the normal ceiling ~1400 so normal payloads and narrate ~197 are untouched, and pulls in the 1500–2430 outlier tail). A truncated structured payload fail-closes via `_structured_call` (bounded blast radius).

**Reversal of a rejected alternative — account-level data policy.** ADR-027 above rejected "relax the account-level data policy" (broad blast radius). QNT-351 found this rejection **forced**: first-party DeepSeek sits behind an **account guardrail** — a raw `only:[deepseek]` call 404s with *"No endpoints available matching your guardrail restrictions and data policy. Configure: openrouter.ai/settings/privacy"* **regardless of per-request `data_collection: allow`**. Reaching it required admitting data-collection providers in the account privacy settings (user decision, 2026-07-08). This **relaxes the account default account-wide**, widening routing eligibility for every alias including the DeepEval judge (ADR-023). Accepted because the public chat carries no PII (ADR-017) and the latency/cost win is material; the per-alias `data_collection: allow` is kept to make the intent explicit at the alias. **Watch**: if any alias ever carries sensitive input, this account-level relaxation — not just the alias flag — must be revisited.

**Watch — non-declarative external state (drift).** The DeepSeek-first pin now depends on an OpenRouter **account privacy setting** (a dashboard toggle, outside this repo) admitting data-collection providers. If it ever drifts back to strict, the three data-retaining pinned providers (deepseek / baidu / alibaba) guardrail-404 and routing silently falls through to novita/gmicloud/deepinfra (all non-caching) or, if all six 404, to the Nemotron anchor — a latency/cost regression, not an outage (litellm's automatic fallback absorbs it), but nothing in-repo detects the drift. The AC6 post-deploy Langfuse check (steady-state `cached_tokens` + p50) is the current detector; a standing Langfuse alert on cache-hit-rate collapse / fallback-rate creep is the durable follow-up as traffic grows (the 1500 `max_tokens` cap's fail-close rate wants the same alert — its derivation window was a small ~4-day / 33-generation sample).

**Strict json_schema holds (smoke receipt).** `scripts/smoke_deepseek_cache_schema.py` re-ran the clarify- and conversational-shaped `ConversationalAnswer` calls under `method="json_schema", strict=True` on the DeepSeek endpoint: **0/8 prose escapes or json_invalid**. QNT-258 had forced `method="function_calling"` on those two paths because DeepSeek returned bare prose under the default json_schema. With `require_parameters: true` routing to enforcing endpoints, strict enforcement now holds — **reverting the `function_calling` workaround is a follow-up candidate** (QNT-351 out-of-scope: needs its own receipts on every structured shape). The economics claim of the Evidence table above (Novita ~0.2× cached input) is superseded by the DeepSeek ~0.02× figure here.
