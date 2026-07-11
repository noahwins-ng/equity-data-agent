# ADR-021: Synthesis stays on llama-3.3-70b; bound the tail with a proxy timeout + fallback

**Date**: 2026-06-11
**Status**: Accepted - model choice superseded by [ADR-025](025-paid-launch-primary-and-breaker-recalibration.md)
**Supersedes/extends**: [ADR-011](011-llm-routing-groq-default-gemini-override.md) (routing topology). Routing decisions for `synthesize`/`narrate` now live here; ADR-011's groq-default / gemini-override / fallback chain otherwise stands.
**Superseded by**: [ADR-025](025-paid-launch-primary-and-breaker-recalibration.md) (QNT-258) - for the public launch the `synthesize`/`narrate` primary moves off Groq `llama-3.3-70b` to the paid DeepSeek V4 Flash primary (Groq is retiring the whole free synthesize-capable chain). The **tail-bounding mechanism** decided here (per-model `timeout: 45` → in-call fallback) is preserved and carried forward.

## Context

QNT-223 (Bundle E of `docs/equity-analyst-improvement-v4.md`) asked two questions about the two heaviest agent LLM calls, `synthesize` and `narrate`:

* **#11 - cache-capable model for synthesis.** Move `synthesize` (and maybe `narrate`) off `equity-agent/default` (Groq `llama-3.3-70b-versatile`, which cannot prompt-cache on Groq) to a cache-capable model, so the large stable prefix (system prompt + force-injected company report) becomes a prefix-cache hit on follow-up turns within a thread.
* **#12 - provider/tail strategy.** The 14-day baseline (2026-05-19 → 06-02, 328 turns) is healthy at the median (p50 5.6s, p90 17.3s) but has a bursty tail: p95 24.4s, **synthesize max 117.5s**, 2.4% of turns over 60s - concentrated on `synthesize`/`narrate` during Groq throttling windows.

This was scoped *after* QNT-220 landed (compact reports + per-node model tiering + the `get_llm(model_alias=...)` hook) and was explicitly conditional on the tail/cost still warranting work once that efficiency work was re-measured.

### Reframing the cache rationale (free tier)

The improvement doc framed #11 as a "token-billing win." **On our free tier that is moot - we are not billed.** Groq's prompt-caching doc does say *"Cached tokens do not count towards your rate limits,"* so the *real* potential win is **TPD relief** (the llama-70b free-tier 100K/day ceiling is the one resource that actually bites us - the whole ADR-011 fallback chain exists because of TPD exhaustion). But that relief has historically mattered during **eval/dev sweeps**, not production traffic, so even in the best case the benefit lands on dev-iteration headroom, not end users.

## Decision

1. **`synthesize` and `narrate` stay on `equity-agent/default` (llama-3.3-70b).** The cache-capable-synthesis lever (#11) is **declined** - not on quality, but on a hard free-tier **capacity/compatibility wall** (evidence below). No cache-capable model can even *serve* the agent's synthesis request on a free tier, so the cache benefit is unrealizable.

2. **Bound the tail (#12) with a LiteLLM proxy-side `request_timeout` that triggers the existing fallback chain.** A throttled `synthesize` call should reroute to the fast Scout fallback within a single client call instead of retrying the same throttled model to ~120s.

3. **Per-node tiering is unchanged from QNT-220:** `classify`/`plan`/exploration-decision on `equity-agent/small` (gpt-oss-20b, small structured calls only); `synthesize`/`narrate` on the 70b default.

### Evidence for declining #11 (measured 2026-06-11)

The agent's synthesis request is **8.4k-12k tokens even after QNT-220 report compaction**. Every free-tier cache-capable path fails *before quality is measurable*:

| Candidate | Cache-capable? | Result | Evidence (live golden run) |
|---|---|---|---|
| gpt-oss-120b / **Groq** | yes (Groq) | ✗ capacity | `GroqException: Request too large ... TPM: Limit 8000, Requested 8990 / 9153 / 12108` |
| gpt-oss-20b / **Groq** | yes (Groq) | ✗ capacity (same wall) | `GroqException: Request too large for openai/gpt-oss-20b ... Limit 8000, Requested 8463` |
| gpt-oss-120b / **Cerebras** | no (not Groq) | ✗ schema incompat | `CerebrasException: Invalid fields for schema with types ['array']: {'minItems','maxItems'}` |

* **Groq free-tier gpt-oss (both 20b and 120b) enforces an 8000-TPM *per-request* ceiling.** A single synthesis call exceeds it and is rejected outright (`please reduce your message size`). This is a real `GroqException` forwarded through LiteLLM, not a bench rate-cap artifact. Only gpt-oss models prompt-cache on Groq, so the cache lever has no viable Groq host.
* **Cerebras** has the token headroom but its structured-output endpoint rejects JSON-Schema array constraints (`minItems`/`maxItems`) that every `Thesis` / `Plan` / `Comparison` output schema uses - and Cerebras gives no Groq prefix caching anyway, so it doesn't serve #11's purpose even if the schemas were stripped.
* **`llama-3.3-70b` serves the same 9-12k requests fine in production** (higher effective ceiling), which is exactly why it remains the synthesize/narrate default.

The earlier-noted quality concern (QNT-129 bench: Groq-hosted gpt-oss-120b "too defensive for primary thesis quality") is now moot for synthesis - the model can't be reached on free tier regardless.

### Evidence / mechanism for #12

QNT-150's 60s client timeout (`LLM_REQUEST_TIMEOUT`, PR #177) was already live during the 14-day baseline, yet `synthesize` maxed at 117.5s. Root cause: LangChain `ChatOpenAI` defaults to `max_retries=2`, so a throttled call burns ~60s, retries, ~60s again ≈ **120s**, then errors - it retries the *same* throttled model instead of rerouting. A proxy-side `request_timeout` shorter than the client timeout makes the proxy abort the throttled upstream and fall through its existing `fallbacks` (→ `fallback-llama4scout`, benched 16/16) inside one client call, bounding worst-case turn wall-clock to roughly `request_timeout + Scout latency` (~50s) with no 120s outliers. Fallback fires only on genuine throttle, so eval stability and the deterministic structured fallbacks are untouched.

## Alternatives Considered

* **Shrink the synthesis request below 8000 tokens** to fit Groq gpt-oss. Reports were already compacted in QNT-220 and synthesis is still 8.4-12k; getting under 8k would mean aggressive further cuts to the report/prefix (QNT-220 territory, with its own quality risk) - out of scope and not worth it for a dev-only TPD benefit.
* **Paid Groq Dev tier** (the upgrade the 429 message advertises) lifts the TPM ceiling. Rejected on the project's "free to clone" constraint (ADR-011); only revisit if the project is ever funded.
* **Cerebras with `minItems`/`maxItems` stripped from the schemas.** Weakens the output contracts for a path that still yields no caching. Rejected.
* **Reduce `ChatOpenAI` max_retries instead of adding a proxy timeout.** Helps the 2× stacking but a single throttled attempt can still run to the 60s client timeout and then *error* rather than reroute. The proxy `request_timeout` + fallback gives a *bounded, successful* turn, which is the actual goal.

## Consequences

**Easier**

* The cache question is closed with hard evidence - no future re-attempt without a paid tier. Recorded in memory (`reference_groq_prompt_caching`). **Update (QNT-318 / [ADR-027](027-prompt-caching-enabled-via-provider-pin.md)):** under paid inference this decline is now *reversed and enabled*. The blocker was never model capability - DeepSeek V4 Flash caches - but OpenRouter load-balancing across ~16 per-provider caches, which an ordered provider pin on `equity-agent/default` fixes (sticky to Novita, ~100% warm prefix cache). Not permanent, and not a free-tier limitation.
* Worst-case turn wall-clock is bounded by config, not luck: a Groq throttle reroutes to Scout instead of hanging to ~120s.
* No quality risk: the hero synthesis call stays on the production-proven 70b; #12 only changes behavior under genuine throttle.

**Harder / watch**

* A `request_timeout` set too aggressively could reroute a legitimately-slow-but-fine call to Scout (a small quality step down on those rare turns). The value is chosen well above p95 (24s) so only true throttle trips it; revisit if Scout-served turns become common in traces.
* The cache/TPD-relief upside for eval-day iteration is forgone. Accepted - it never reached production users.
* `synthesize`'s 8.4-12k token size is now a known constraint on any future model swap: any candidate must clear a >12k single-request budget on its free tier.

## Acceptance-criteria status

* **AC1 (ADR)** - this document. ✓
* **AC2 (quality gate on a swap)** - **N/A**: no model swap is made. The quality eval was never reached because all cache-capable candidates failed the capacity/compat gate first (documented above). ✓ (resolved by capacity, not quality)
* **AC3 (cache OR fallback effect, execution)** - the cache branch is dead; the fallback branch is the live path. ✓ **demonstrated in dev (2026-06-11):** with `timeout: 3` on `equity-agent/default`, a generation that exceeded the timeout was rerouted to `meta-llama/llama-4-scout-17b-16e-instruct` and returned successfully (`finish: stop`); with the production `timeout: 45`, a normal request stayed on llama-3.3-70b (0.35s, no reroute). The proxy bounds a throttled call to ~48s instead of the ~120s retry-stack.
* **AC4 (tail re-measure, execution)** - pending a post-deploy clean-window run of `langfuse_baseline`.
* **AC5 (no p50 regression, execution)** - pending the same re-measure. (#12 changes only the throttled tail, so median is expected flat.)

## QNT-227 follow-up: the Cerebras fallback was removed (2026-06-11)

ADR-021 noted in passing that Cerebras "timed out" on synthesize during the QNT-223 eval. QNT-227 verified this with a controlled run and acted on it.

**Method.** Real NVDA synthesis payload (12.9 KB of live reports + the system prompt, the documented 9-12k-token regime), routed through the isolated `equity-agent/bench-cerebras-gptoss120b` alias (no fallback, bounded only by the 60s `LLM_REQUEST_TIMEOUT`). This is faithful to the prod blackout path: Cerebras is only reached when both Groq buckets return fast 429s, so it gets the full client budget - exactly what the isolated full-budget call measures.

**Result - Cerebras cannot serve synthesize within the timeout.**

| call | result | elapsed |
|---|---|---|
| `bench-llama4scout` (the fallback *in front* of Cerebras) | ✓ ok | 3.5s |
| `bench-cerebras` trial 1 | over timeout | 61.9s |
| `bench-cerebras` trials 2-4 | `APITimeoutError` | ~181s each |

0/4 within 60s. With no per-model timeout, a throttled-into-Cerebras synthesize stacks 3 client retries to **~181s** before erroring - a *worse* unbounded tail than the ~120s ADR-021 (#12) set out to kill. The Cerebras fallback, as configured, defeated the ADR's own tail-bounding goal in the double-blackout case.

**litellm fallbacks are recursive (corrects a QNT-215/QNT-227 assumption).** The plan path uses the array-bounded `ThesisPlan` schema (`min_length`/`max_length` on `tools`) and runs on `equity-agent/small`, whose chain is `[default, scout]` - both Groq. QNT-227 assumed that meant `ThesisPlan` could never reach Cerebras. It can: verified empirically (poisoned-Groq / valid-Cerebras proxy, `small`-only call) that litellm walks each fallback's *own* fallback list, so `small → default → (default's chain) → cerebras`. The proxy logged the `CerebrasException: Invalid fields for schema ... {minItems, maxItems}` rejection. Benign in practice (the graph's deterministic all-tools fallback catches a failed `ThesisPlan`), but the "no array-bounded schema can reach Cerebras" premise is false.

**Decision - drop Cerebras from every chain** (chosen over a per-model timeout or accept-as-is). It served no call it could be reached for: synthesize times out, `ThesisPlan` is array-rejected, and the final fallback `groq/gpt-oss-120b` already covers the small plan/classify calls (it *fast-fails* a 9-12k synthesize on its 8000-TPM per-request ceiling rather than hanging). The "provider-level quota diversity" QNT-215 wanted was illusory for the calls that actually reach it. Chains are now `default → scout → groq-gptoss` and `scout → groq-gptoss`. The `bench-cerebras-gptoss120b` alias is untouched (it backs the dialogue-eval judge and the QNT-129 bench). No normal-traffic behavior change - the removed hop was only reachable in a double-TPD-blackout.
