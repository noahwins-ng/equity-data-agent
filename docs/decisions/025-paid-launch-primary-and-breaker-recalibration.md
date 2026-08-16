# ADR-025: Paid inference primary for public launch - DeepSeek V4 Flash via OpenRouter + breaker recalibration

**Date**: 2026-07-04
**Status**: Accepted
**Supersedes/extends**: [ADR-021](021-synthesis-model-and-tail-routing.md) (synthesize/narrate model choice - the "stays on llama-3.3-70b" decision is superseded for the launch config) and [ADR-011](011-llm-routing-groq-default-gemini-override.md) (routing topology - the groq-default provider slot is repointed; the gemini override stands). Relaxes the "free-tier only on the chat path" cost model of [ADR-017](017-public-chat-truly-public-no-auth.md).

## Context

The agent chat is being shared publicly (LinkedIn, ~10-20 concurrent visitors). A load review found the Hetzner CX41 host and the async SSE path handle that concurrency fine - the binding constraint was the **Groq free-tier daily token ceiling**, not CPU/RAM.

Under ADR-021, `synthesize`/`narrate` ran on Groq `llama-3.3-70b-versatile` (free tier, 100K TPD) with the global TPD circuit breaker (`CHAT_TOKENS_GLOBAL_PER_DAY`) sized at ~50% of that ceiling. At ~12-15K tokens per substantive thesis chat, the 200K breaker trips after **~15 chats** - after which every new visitor gets the degraded "daily ceiling reached" card until UTC midnight. Not a crash; a silent quota wall on launch night.

Two things force a provider change rather than a tuning change:

1. **Free-tier economics don't survive a public launch.** Even raising the breaker only trades the quota wall for provider-side 429s once the free TPD is gone.
2. **Groq is retiring the entire synthesize-capable free chain** (deprecation emailed 2026-06-17, applies to free *and* on-demand paid tiers; only enterprise committed-spend is exempt): `llama-4-scout` + `qwen3-32b` on 2026-07-17, `llama-3.3-70b` + `llama-3.1-8b-instant` on 2026-08-16. Survivors (gpt-oss-120b/20b, qwen3.6-27b) all cap at 8000 TPM per request and **413 on a 9-12K synthesize** (the wall ADR-021 documented). So a same-provider billing flip to paid Groq 70b was not available, and Groq has **no** free synthesize-capable host after 08/16.

## Decision

### 1. The launch primary is DeepSeek V4 Flash via OpenRouter (paid)

`equity-agent/default` (synthesize/narrate) moves from Groq `llama-3.3-70b-versatile` to `openrouter/deepseek/deepseek-v4-flash`. DeepSeek V4 Flash is $0.09/$0.18 per M (~$0.002/chat, ~6× cheaper than paid llama-3.3-70b), 1M context, non-reasoning, and **structured-outputs capable** - already exercised in production as the QNT-275 / ADR-023 DeepEval judge, so the OpenRouter key + wiring already exist. Paid removes the daily cap; at launch scale (~$1/day) cost is not the constraint, reliability is.

The [ADR-021](021-synthesis-model-and-tail-routing.md) tail guard is preserved: the per-model `timeout: 45` on the default alias still aborts a throttled primary call and reroutes to the fallback anchor within one client call, bounding worst-case turn wall-clock to ~48s.

### 2. Fallback anchor is a free OpenRouter model (Nemotron 3 Ultra)

Because Groq no longer offers a free synthesize-capable host, the fallback anchor cannot be a surviving Groq model. The chain is:

```
equity-agent/default (DeepSeek V4 Flash, paid)
  → equity-agent/fallback-nemotron-ultra (nvidia/nemotron-3-ultra-550b-a55b:free)
  → (chain exhausted → graph.py deterministic fallback, fail-closed)
```

- **Nemotron 3 Ultra** (1M ctx) is **structured-outputs capable** on OpenRouter, so it serves both the array-bounded `ThesisPlan` synthesize (the schema Cerebras was rejected on, [QNT-227](021-synthesis-model-and-tail-routing.md)) *and* narrate. It is the true synthesize-capable anchor. Verified live (clean completion, `finish_reason=stop`).
- **Laguna M.1** was evaluated as a second anchor and **dropped in review** (QNT-258): `poolside/laguna-m.1:free` returned upstream `429`s and empty completions on a live smoke test, so a known-flaky last hop only added retry latency ahead of the deterministic fallback. If Nemotron also fails, `graph.py::_structured_call` returns `None` and the caller degrades (fail-closed) - no crash.

**Free-model boundary - and the deliberate exception.** The QNT-258 discussion set free models (OpenRouter Nemotron, `:free` variants, OpenCode Zen) as **bench-harness-only**: they have shared ~20 RPM / 50-1000 RPD caps, ephemeral/donated capacity, and **train on submitted prompts** (real user queries). That boundary holds for the **primary** (which stays paid). It is deliberately **overridden for the fallback slot only** (user decision): the anchor carries only the small residual traffic the paid primary dropped on a rare throttle/timeout, so the rate-limit and train-on-prompts exposure is bounded to fallback events, not the hot path. A permanent paid multi-provider gateway is noted as launch-day insurance only and **not** adopted here (out of scope).

### 3. Global breaker recalibrated for paid economics

`CHAT_TOKENS_GLOBAL_PER_DAY`: **200K → 20M**. On a paid plan there is no free-tier TPD to proxy, so the breaker stops meaning "free tokens left" and becomes a pure **runaway-cost / abuse circuit breaker**. Sizing: a substantive chat is ~14K tokens at ~$0.002; the launch envelope is ~$1/day (~500 chats); 20M is ~2.8× that (~1,400 chats, ~$2.7/day worst-case ceiling) - above a good launch evening + daily ingest + dev/eval sweeps, yet still bounding a stuck loop or scraper to a few dollars/day. It **still fails closed**.

The **per-user anti-abuse fences are unchanged** and do the real work: per-IP token budget (30K/day) and per-IP rate limit (5/min; 30/hour; 100/day). A single IP can burn at most 30K tokens/day; reaching the 20M global cap through legitimate per-IP-capped traffic needs hundreds of distinct IPs (a viral overshoot or coordinated abuse) - exactly what a global breaker should catch.

## Alternatives Considered

- **Groq `llama-3.3-70b` flipped to on-demand paid** - the safest option (zero re-bench, keeps the ADR-021-calibrated model). **Invalidated** by the 2026-06-17 decommission: on-demand paid loses the model on 08/16; only enterprise committed-spend keeps it, not viable for a demo.
- **Free OpenRouter model as the primary** (Nemotron `:free`, llama-3.3-70b:free) - rejected for the hot path: shared RPM/RPD caps throttle a launch burst (each turn is 3-4 LLM calls) and they train on real user prompts. Valid only in the bench harness (no user data, zero cost) and, per the override above, in the rare fallback slot.
- **Convert the breaker to a dollar/day spend cap** - more precise, but requires per-token pricing + dollar accounting in `TokenBudget`. The token-cap recalibration is the minimal change AC4 permits ("or its dollar-cap replacement"); the comment expresses the derivation in dollars so intent stays legible. Revisit if OpenRouter model-mix pricing drifts.
- **Cerebras** stays out ([QNT-227](021-synthesis-model-and-tail-routing.md)): cannot serve synthesize within the client timeout, and rejects the array-bounded schema.

## Consequences

- **Launch survives the burst.** The quota wall at ~15 chats is gone; the breaker now sits ~100× higher and is a cost guard, not a capacity wall.
- **"Free to clone" is relaxed for the default config.** A cloner running the default provider now needs an `OPENROUTER_API_KEY`. The free `equity-agent/gemini` override (ADR-011) remains a one-line switch for a zero-cost clone, and the fallback anchors are free. This is the intended trade for a public, paid-primary launch.
- **The fail-closed safety test changes shape, not intent.** `tests/agent/test_litellm_fail_closed.py` no longer asserts "free-tier only"; it asserts "permitted providers only" (groq/gemini/google/openrouter) and still **forbids frontier paid providers** (anthropic, direct openai, azure, bedrock, vertex, …) on the entire reachable chat chain. The runtime fail-closed contract (a quota error degrades to a `ConversationalAnswer`, never propagates) is unchanged.
- **The Groq fallback aliases are now off the default chain but still wired for the `small` tier.** The full fallback + eval-baseline re-anchoring (Groq bench aliases point at retiring models; `small`'s `fallback-llama4scout` hop dies 07/17) is a follow-up pass, out of scope here.
- **Reasoning defaults ON on OpenRouter - must be disabled.** DeepSeek V4 Flash on OpenRouter is served with reasoning ON by default (37-89 CoT tokens/call even at `reasoning_effort: low`), contradicting the ticket's "non-reasoning" premise. Left on, it produced a degenerate bench (~30 min), intermittent empty completions, and uniform narrate grounding misses. The fix - `extra_body: {reasoning: {enabled: false}}` on the `default` and bench aliases - is a hard requirement of this decision, not a tuning nicety. Verified: `reasoning_tokens → 0`, clean output.

- **Quality gate PASSED (2026-07-04, reasoning-off).** The golden sweep (41 records, `docs/model-bench-2026-07.md`) shows DeepSeek V4 Flash meets or beats the llama-3.3-70b baseline on every hard gate: hallucination_ok 40/41 vs 39/41, judge composite 5.634 vs 4.293, cosine 0.431 vs 0.408, p50 15.3s vs 14.3s (parity), and a **far tighter p90 tail (36.8s vs 128s)** - the throttled-tail failure mode ADR-021 targeted. Promoted to `equity-agent/default`. Remaining checks (routing_eval AC8, narrate A/B AC9, production p50 AC6) are recorded/deferred in the bench doc.

## Amendment (QNT-442, 2026-08-16): moved to the 0731 GA build

DeepSeek shipped `DeepSeek-V4-Flash-0731` (2026-07-31), the GA release of the
same 284B/13B-active MoE this ADR pinned as a preview (`deepseek-v4-flash`,
undated, resolves to the 0423 build) -- a re-post-trained revision focused on
agentic/coding/tool-use, not a new model generation. `equity-agent/default`
and `equity-agent/bench-deepseek-v4-flash` (ADR-023) both moved to
`openrouter/deepseek/deepseek-v4-flash-0731` in lockstep -- a judge/primary
version mismatch would make prod behavior and DeepEval scores diverge
silently, so they never move independently.

**Pricing**: OpenRouter's aggregate moved from $0.089/$0.224 to $0.08/$0.252
per M in/out -- input cheaper, output ~12% pricier. Given the input-dominated
~12:1 prod traffic skew this ADR's economics section measured (QNT-292), net
cost impact is roughly flat.

**Smoke receipts (`scripts/smoke_openrouter_providers.py`,
`scripts/smoke_deepseek_cache_schema.py`), re-run against 0731**: all 6 pinned
providers (deepseek, baidu, alibaba, novita, gmicloud, deepinfra) serve every
arm reasoning-off, 3/3 clean; reasoning-off still holds (`reasoning_tokens: 0`
on a live probe); the deepseek-first prefix cache still warms (`cached_tokens`
0 -> 4096 across two back-to-back calls); strict `json_schema` still holds
(0/6 prose escapes). None of the QNT-318/QNT-351 provider-pin assumptions
broke on the version bump.

**`max_tokens` re-derived, left unchanged at 1500 -- corrected after review
caught a wrong target.** `Thesis` synthesize output did grow measurably more
verbose on 0731 (9 live-sampled real thesis calls: `completion_tokens`
min=1301 median=1514 p90=1581 max=1601), and a first pass of this ticket
read that as grounds to raise the alias-level `max_tokens: 1500` -> `2000`.
That was the wrong target: since QNT-383, every structured schema
(`Thesis` included) is sized from its own dedicated `_OUTPUT_BUDGET` entry in
`graph.py::_structured_call` (`Thesis: 2500`), which overrides this alias
default per-request -- so the alias-level cap never bounded `Thesis` in the
first place, and the "4 of 10 calls truncated at 1500" reading came from a
probe that bypassed `_structured_call` entirely. The alias-level cap's only
live consumer is `narrate_node`'s free-text stream (no per-call override).
Re-sampled against narrate's real shape (`build_narrate_prompt`, real report
payloads, 8 calls, uncapped): `completion_tokens` min=143 median=211 p90=273
max=319 -- no verbosity-growth pressure on the thing this parameter actually
gates. Left at 1500. See `litellm_config.yaml`; `Thesis`'s own headroom is
tracked separately in `graph.py`'s `_OUTPUT_BUDGET` table, untouched by this
ticket.

**Quality signal is mixed -- recorded honestly rather than cherry-picked.**
Three independent golden-bench runs (`golden_history.csv`,
`bench-deepseek-v4-flash`, n=44 each, spanning ~13 hours, including one
SHA-accurate confirming run after the AC3 fix) agree closely with each other
(composite 5.20, 4.95, 5.27) but all three sit **below** the 0423 baseline
this ADR recorded (composite 5.63, n=41) -- consistent across three
independent samples, this reads as a real, stable regression on this metric,
not noise. `tool_call_ok` improved to 100% (was 97.6%) and `hallucination_ok`
held flat (42/44 vs 40/41), but the `structure` axis dropped (1.95 -> ~1.3-1.6)
on repeated verdict-label-consistency misses ("verdict_rationale must mention
at least one aspect label verbatim"). Latency is noisier and less clearly
regressed than it first looked: mean elapsed 20.5s (baseline) vs 30.1s /
22.2s / **19.5s** (mean_elapsed_ms) and p90 31.3s (baseline) vs 50.7s / 48.5s
/ **36.2s** across the three runs -- the third, cleanest run landed close to
baseline, so the earlier two elevated readings likely reflect session/load
variance (heavy back-to-back testing) rather than a stable 0731 latency
regression. The first two runs independently flagged 2-3/44 records over a
60s timeout-ceiling as possible "provider-pressure contamination" per the
script's own gate; the third (clean-window) run flagged none. Against this,
the DeepEval RAGAS suite (see ADR-023 amendment) improved on
every metric -- the two eval methodologies are measuring different things
(a free-judge structured-answer bench vs a paid-judge RAGAS suite on a
recall-oriented golden set) and are allowed to disagree.

**One new failure mode observed, not present in the original QNT-258/351
characterization**: during the golden bench re-run, the agent's own
comparison-synthesize call (not the judge) hit a `LengthFinishReasonError` /
JSON-parse failure on 0731 and degraded to a conversational fallback
(bounded blast radius, existing design holds) -- worth a watch item, not a
blocker.

**No prod traffic exists yet for 0731** (pre-deploy, still on a feature
branch) -- everything above is sampled/bench-derived, not the
Langfuse-observed prod distribution the original 1500 cap used. Re-derive
`max_tokens` and re-check the golden-bench composite drop against real
traffic once 0731 has been live a few days.

**Provenance caveat, disclosed rather than hidden (review finding, corrected
across three rounds -- final version kept deliberately narrow to the
checkable facts).**

Facts (each verified directly against `golden_history.csv` /
`deepeval_history.csv` and `git log`, not reconstructed from memory):

- `git_sha=d7b9f75` is recorded for three of the four receipt rows:
  `20260815T182212Z-41bdbb` (DeepEval), `20260815T182236Z-0edde6` (golden
  bench), `20260816T030527Z-dd296d` (golden bench). `d7b9f75` is the direct
  parent of `f752324` (committed 2026-08-16T04:09:39Z), the commit that
  pins `equity-agent/default` / `equity-agent/bench-deepseek-v4-flash` to
  `-0731`. All three runs finished before `f752324` was committed, so all
  three recorded SHAs are stale: checking out `d7b9f75` today gets the 0423
  preview, not 0731 -- the SHA-vs-runtime-identity gap this project treats
  seriously elsewhere.
- Only `20260816T043548Z-db81e1` (golden bench) has `git_sha=4490d87`, a
  commit after `f752324` -- itself the AC3 `max_tokens` fix -- accurate.
- Despite the stale labels, the -0731 pin genuinely was live in the proxy
  for all four runs (live-verified via proxy probes showing `provider:
  DeepSeek` at multiple points across this session, including immediately
  before the DeepEval run and again before `dd296d`) -- it was applied as
  an uncommitted local edit well before any of these runs and only
  committed afterward. So the scored numbers are real 0731 output; only the
  SHA label on three of the four rows is wrong.

What this means in practice: `db81e1`'s accurate-SHA composite (5.27)
agrees closely with the two mislabeled golden-bench runs (5.20, 4.95), so
golden-bench is cross-validated by a clean receipt. The DeepEval row has no
such confirming re-run -- the 2+ hour sweep was not re-run purely to fix a
label, because the state `41bdbb` actually tested (the -0731 pin,
uncommitted at the time, at `max_tokens=2000`) differs from current HEAD
(the same pin, now committed, at `max_tokens=1500`) in exactly one way that
matters: the AC3 `max_tokens` value. That value has zero effect on how
DeepEval scores `Thesis`-shaped output (sized from `graph.py`'s independent
`_OUTPUT_BUDGET` table, not this alias default -- see the AC3 note above).
All three mislabeled rows are kept as audit trail rather than silently
corrected.
