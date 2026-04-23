# ADR-011: LLM Routing — Groq (default) + Gemini 2.5 Pro (quality override) via LiteLLM

**Date**: 2026-04-23
**Status**: Accepted (revised same-day from a Claude-override variant — see §"Revision history")

## Context

QNT-59 sits at the root of the Phase 5 dependency chain: the `get_llm()` factory it provides is consumed by the LangGraph graph (QNT-56), the tools (QNT-57), the CLI (QNT-60), and the eval harness (QNT-67). Before the first line of agent code, we need to pick which provider the project's `equity-agent/default` alias actually routes to.

The architectural indirection — LiteLLM proxy with a model alias consumed by the agent — is already decided (see repo `litellm_config.yaml` and the LLM Routing section of `docs/architecture/system-overview.md`). What this ADR resolves is **which provider(s) sit behind the alias**.

Project constraints:

- **Free tier required.** This is a portfolio project, not a business. A recruiter opening the README a year from now should be able to clone the repo and run `python -m agent analyze NVDA` without a paid API bill. That constraint rules out Claude, GPT-5/4o, and any other paid-from-dollar-one provider as the default OR the override.
- **Host memory budget tight** — Hetzner CX41 is ~14.75 / 16 GB already allocated after QNT-116's Dagster topology migration (see ADR-010). Adding a 6-GB self-hosted Ollama container is not an option.
- **Eval harness concurrency** — QNT-67 runs ≥10 golden-set questions against the CLI per eval; ideally we can run several in parallel to keep iteration fast.
- **Quality tier needed for hero artifact** — one of the project's portfolio artifacts is a "read this thesis" screenshot (QNT-66 README). The default provider's quality doesn't have to top the leaderboard, but the override provider should be genuinely frontier so the recruiter-facing screenshot is representative.
- **No vendor lock-in** — LiteLLM already abstracts this; any decision here should stay one YAML edit away from reversal.

## Decision

**Default: Groq** (llama-3.3-70b-versatile at Phase 5 start). **Quality override: Google AI Studio, Gemini 2.5 Pro.** Both free tier, no credit card required.

Concretely:

- `litellm_config.yaml` defines `equity-agent/default` → Groq (via `GROQ_API_KEY`).
- When `GEMINI_API_KEY` is set and the runtime env var `EQUITY_AGENT_PROVIDER=gemini` is passed, LiteLLM routes the same alias to Gemini 2.5 Pro. One code path, two backends.
- Agent code references only the alias. Switching is config-only — no agent-side import, no SDK swap.
- Eval harness (QNT-67) takes a provider axis so the golden-set regression report has per-provider columns; this turns the dual-provider setup from "override for demos" into "deliberate evaluation signal".

### Free-tier budget

| Provider | Model | Limits (free, no card) | Fit |
|---|---|---|---|
| **Groq (default)** | llama-3.3-70b-versatile | 30 RPM / 6K TPM / up to 14.4K RPD, ~500 tok/s | Covers Phase 5 dev iteration, eval-harness batch runs, steady-state portfolio demos |
| **Gemini (override)** | Gemini 2.5 Pro | 5 RPM / 100 RPD, 250K TPM universal cap | Hero demo thesis, README screenshot, 20-question golden-set cross-check |

100 RPD on Gemini Pro covers: one hero demo + 20 golden-set eval questions + ~80 recruiter-triggered runs per day. If that ceiling is hit, **Gemini 2.5 Flash** (free tier: 15 RPM / 1500 RPD) is the fallback — bigger volume at a small quality step down, same key, same provider.

## Alternatives Considered

**Ollama Cloud as default (original QNT-59 plan).**

- Free tier is **subscription-shaped**: 1 concurrent model; Pro at $20/mo unlocks 3. Usage measured in GPU-time, not tokens, so cost is harder to predict from prompt-size math.
- The 1-concurrent-model limit would serialise the QNT-67 eval harness when running 10-20 golden-set questions, turning a ~2-minute eval into a ~20-minute eval. That directly hurts Phase 5's iteration speed, which is the whole reason Langfuse (QNT-61) is day-one.
- Rejected on the concurrency ceiling.

**Anthropic Claude Sonnet 4.6 as override (first draft of this ADR).**

- Genuinely frontier quality; best reasoning depth on long context.
- Paid from dollar one. Estimated Phase 5 dev cost: ~$33 if any meaningful fraction of runs route to Claude. Fine for a funded product, wrong shape for a portfolio project that wants to stay free to clone.
- Quality gap over Gemini 2.5 Pro is marginal on the kinds of analytical-synthesis tasks the agent actually does; the thesis isn't reasoning-intensive enough to need Opus-class depth.
- Rejected on the "free to clone" constraint. Stays on the shortlist if the project ever grows into anything paid, since LiteLLM can add Claude behind the same alias with a YAML edit.

**OpenAI (GPT-5 / GPT-4o / GPT-4o-mini) as override.**

- No free tier of meaningful volume. 4o-mini is cheap (~$0.003/analysis) but still paid-per-token from dollar one.
- Same "paid from dollar one" disqualification as Claude. Valid choice if we ever need a second paid API relationship for comparison.

**Gemini 2.5 Pro as default instead of Groq.**

- Free-tier RPD (100) is below what Groq offers (up to 14.4K). Prompt-iteration loops during Phase 5 dev would hit the daily ceiling within an hour of serious work and block for 24 hours.
- Flash tier (1500 RPD) would be enough for volume but loses the "frontier quality override" story — we'd be picking Flash both places.
- Gemini is the right quality-tier pick, not the right default-tier pick, given the volume asymmetry.

**OpenRouter as default or override.**

- Aggregator over 100+ models. Free-tier available for some open models but inventory is volatile — which model is "free" on OpenRouter shifts month to month.
- Attractive for the eval harness specifically (one key → many models), but adds a third-party in the request path and makes the "this model was used for the README screenshot" claim less stable.
- Defer to a future decision if QNT-67 grows into a cross-provider comparison harness worth the indirection overhead.

**Self-hosted Ollama on Hetzner.**

- ~6 GB RAM for a 7B-class model, more for 70B-class. Would re-open the QNT-111/113/115/116 memory-pressure cycle on a host whose budget is already ~14.75/16 GB committed. Rejected on infra.
- The self-host-later escape hatch remains: Ollama has the same OpenAI-compatible API shape, so swapping provider → self-hosted Ollama is still a YAML edit if the need arises.

## Consequences

**Easier:**

- **$0 marginal cost** for the entire project. Phase 5 dev, steady-state portfolio demos, and the occasional recruiter-triggered run all fit inside free-tier budgets on both providers.
- **Eval harness can parallelise.** Groq's per-request rate limit (30 RPM) is the binding constraint on the default tier, not concurrency; QNT-67 can run 10 questions concurrently without hitting the 1-model ceiling the way Ollama Cloud free would.
- **Iteration speed.** Groq ships inference at ~500 tok/s; a 2K-output thesis returns in ~4 s, shaving prompt-debug loops compared to Ollama Cloud or Gemini Flash.
- **Quality override stays one YAML edit away.** Gemini 2.5 Pro as the override means the recruiter-facing thesis in the README screenshot (QNT-66) and the hero demo (QNT-94) use a frontier-leaderboard model without the project's API surface or eval harness changing.
- **Eval harness becomes a provider-comparison artifact.** Logging per-provider columns in `evals/history.csv` (QNT-67) turns the Groq↔Gemini split into a measurable AI-engineering signal rather than an invisible default.
- **Two-provider diversity.** Different model families (llama vs Gemini), different training stacks, different hosting infrastructures. A genuinely cross-provider eval run rather than two flavours of the same backend.

**Harder:**

- **Two API keys to manage** (`GROQ_API_KEY`, `GEMINI_API_KEY`) instead of one. SOPS already handles prod (QNT-102); dev `.env.example` gets a second line.
- **Gemini 2.5 Pro RPD is tight** (100 RPD free). Enough for hero demo + one golden-set eval run + a handful of recruiter-triggered runs per day, but a full QNT-67 batch across 20 questions × 2 providers would eat 20 of those 100. Fallback: flip to Gemini 2.5 Flash (1500 RPD) if the quality gap is acceptable.
- **Groq model catalog churns.** Groq rotates which models they host; `llama-3.3-70b-versatile` today could be deprecated in six months and replaced. Mitigation: the `equity-agent/default` alias in `litellm_config.yaml` is the one place to update — agent code is unaffected.
- **Free-tier rate limits are a real ceiling for bursty backfills.** 30 RPM (Groq) means an eval harness running 20 questions back-to-back needs ~40 s; a 100-question batch would rate-limit. Upgrade path is Groq Developer tier (10× limits, $25/mo free credit, credit card required) — re-evaluate when/if we hit the ceiling.
- **Data residency.** Free tiers on both providers run in US regions. Acceptable for public market data; flag if project ever handles anything private.
- **Quality ceiling below Opus/GPT-5.** Gemini 2.5 Pro is competitive with frontier models but not always the leader for a given task. Anything demanding Opus-class reasoning depth (which this project doesn't currently need) would want Claude or GPT-5 layered in as a third option — LiteLLM makes that a one-config addition.

## Revisit triggers

Reopen this ADR if any of these fire:

- Groq free-tier RPD ceiling hit during normal Phase 5 dev (bump to Developer tier or swap default to Gemini Flash).
- `llama-3.3-70b-versatile` deprecated from Groq without a clear successor.
- Gemini 2.5 Pro 100 RPD proves too tight for the eval-harness cadence even with parallel provider runs (fall back to Gemini 2.5 Flash at 1500 RPD for the override slot).
- QNT-67 eval harness shows a qualitatively different thesis shape on Gemini vs Groq large enough that a Groq default produces unrepresentative demo artifacts.
- Project scope expands to multi-turn conversational agents where the in-memory LangGraph checkpointer (implicit in the current design) stops being enough — re-examine provider choice alongside checkpointer persistence.
- Project gets funded / monetised such that a paid provider like Claude or GPT-5 becomes a reasonable quality-override addition.

## Revision history

**2026-04-23 (initial, same-day revision):** First draft placed Claude Sonnet 4.6 in the override slot. Revised before shipping because the project's portfolio-not-product framing calls for $0 marginal cost — a visitor cloning the repo in 6 months should be able to run the full stack without a paid API relationship. Swapped override to Gemini 2.5 Pro (free tier, 5 RPM / 100 RPD, no credit card). Claude/Opus/GPT-5 remain on the shortlist as layerable additions if the project ever acquires a budget.

**2026-04-23 (Pro → Flash, post-QNT-59):** First live end-to-end test of the override path (prod, hours after QNT-59 shipped) returned:

```
HTTP 429: Quota exceeded for metric:
  generativelanguage.googleapis.com/generate_content_free_tier_requests,
  limit: 0
```

`limit: 0` is not a rate-window that clears on wait — it's Google AI Studio cutting off Gemini 2.5 Pro from the free tier entirely. That breaks this ADR's core "free to clone" invariant: a visitor cloning the repo with only a free Gemini key can't exercise the override at all. The quoted "5 RPM / 100 RPD" free-tier numbers for Pro (sourced from docs when this ADR was drafted same-day) are no longer reality for new keys.

Demoted the override to **Gemini 2.5 Flash** (15 RPM / 1500 RPD, confirmed free-tier-reachable). Flash is a capable 2025-era model — the per-provider axis Groq-Llama vs Google-Gemini remains meaningful, and Flash's higher RPD ceiling actually makes QNT-67's eval harness more comfortable than Pro would have been even if Pro were still free. Pro stays available as a one-line YAML edit in `litellm_config.yaml` if a paid Gemini plan is ever added — no ADR amendment needed for that flip.

Tracked under QNT-123. This is exactly the `feedback_vendor_prod_docs.md` lesson firing: vendor tier assumptions must be re-verified at ship time, not trusted from docs copied into an ADR the same day the docs were read.
