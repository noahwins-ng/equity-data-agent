# ADR-011: LLM Routing — Groq (default) + Claude (override) via LiteLLM

**Date**: 2026-04-23
**Status**: Accepted

## Context

QNT-59 sits at the root of the Phase 5 dependency chain: the `get_llm()` factory it provides is consumed by the LangGraph graph (QNT-56), the tools (QNT-57), the CLI (QNT-60), and the eval harness (QNT-67). Before the first line of agent code, we need to pick which provider the project's `equity-agent/default` alias actually routes to.

The architectural indirection — LiteLLM proxy with a model alias consumed by the agent — is already decided (see repo `litellm_config.yaml` and the LLM Routing section of `docs/architecture/system-overview.md`). What this ADR resolves is **which provider(s) sit behind the alias**.

Project constraints:

- **Free tier preferred** — portfolio project, ~500 analyses during Phase 5 dev + ~100/month steady-state from recruiter demos.
- **Host memory budget tight** — Hetzner CX41 is ~14.75 / 16 GB already allocated after QNT-116's Dagster topology migration (see ADR-010). Adding a 6-GB self-hosted Ollama container is not an option.
- **Eval harness concurrency** — QNT-67 runs ≥10 golden-set questions against the CLI per eval; ideally we can run several in parallel to keep iteration fast.
- **Quality tier needed on demand** — one of the project's portfolio artifacts is a "read this thesis" screenshot (QNT-66 README). The default provider's quality doesn't have to top the leaderboard, but the override provider does.
- **No vendor lock-in** — LiteLLM already abstracts this; any decision here should stay one YAML edit away from reversal.

## Decision

**Default: Groq** (llama-3.3-70b-versatile at Phase 5 start, revisit if a better open model lands on Groq). **Override: Anthropic Claude Sonnet 4.6.**

Concretely:

- `litellm_config.yaml` defines `equity-agent/default` → Groq (via `GROQ_API_KEY`).
- When `ANTHROPIC_API_KEY` is set and the runtime env var `EQUITY_AGENT_PROVIDER=claude` is passed, LiteLLM routes the same alias to Claude Sonnet. One code path, two backends.
- Agent code references only the alias. Switching is config-only — no agent-side import, no SDK swap.
- Eval harness (QNT-67) takes a provider axis so the golden-set regression report has per-provider columns; this turns the dual-provider setup from "override for demos" into "deliberate evaluation signal".

## Alternatives Considered

**Ollama Cloud as default (original QNT-59 plan).**

- Free tier **subscription-shaped**: 1 concurrent model; Pro at $20/mo unlocks 3. Usage measured in GPU-time, not tokens, so cost is harder to predict from prompt-size math.
- The 1-concurrent-model limit would serialise the QNT-67 eval harness when running 10-20 golden-set questions, turning a ~2-minute eval into a ~20-minute eval. That directly hurts Phase 5's iteration speed, which is the whole reason Langfuse (QNT-61) is day-one.
- Rejected on the concurrency ceiling, not the pricing.

**OpenAI (GPT-5 / GPT-4o / GPT-4o-mini) as default.**

- No free tier of meaningful volume. 4o-mini is cheap (~$0.003/analysis) but still paid-per-token from dollar one.
- Valid choice if we wanted a single provider with both quality and cheap tiers; adds a second paid API relationship over the `ANTHROPIC_API_KEY` we already need.
- Kept as a possible override target via LiteLLM, not a default.

**Google Gemini 2.5 Flash as default.**

- Best free tier in absolute volume (1500 RPD, 15 RPM on AI Studio). Competitive quality on 2.5 Flash, very strong on 2.5 Pro.
- Rejected narrowly in favour of Groq on inference speed — Groq's ~500 tok/s on llama-3.3-70b meaningfully reduces prompt-iteration wait during Phase 5 dev, which Gemini Flash doesn't match. Gemini stays on the shortlist as an override target if Groq rate-limits become a problem.

**OpenRouter as default.**

- Aggregator over 100+ models including Claude, GPT, Gemini, llama. Pay-per-token with a small markup.
- Attractive for the eval harness specifically (one key → many models), but adds a third-party in the request path for every production call. Defer to a future decision if QNT-67 grows into a cross-provider comparison harness worth the latency overhead.

**Self-hosted Ollama on Hetzner.**

- ~6 GB RAM for a 7B-class model, more for 70B-class. Would re-open the QNT-111/113/115/116 memory-pressure cycle on a host whose budget is already ~14.75/16 GB committed. Rejected on infra.
- The self-host-later escape hatch remains: Ollama has the same OpenAI-compatible API shape, so swapping provider → self-hosted Ollama is still a YAML edit if the need arises.

## Consequences

**Easier:**

- **Free-tier covers Phase 5 dev.** 30 RPM / 6 K TPM / up to 14.4 K RPD (Groq free, email-only signup) is above our bursty prompt-iteration peaks. Phase 5 dev cost stays near $0 until we deliberately switch to Claude for the eval harness or demo.
- **Eval harness can parallelise.** Groq's per-request rate limit (30 RPM) is the binding constraint, not concurrency; QNT-67 can run 10 questions concurrently without hitting a 1-model ceiling the way Ollama Cloud free would.
- **Iteration speed.** Groq ships inference at ~500 tok/s; a 2 K-output thesis returns in ~4 s, shaving prompt-debug loops compared to Ollama Cloud or Gemini Flash.
- **Quality override stays one YAML edit away.** Claude Sonnet 4.6 as an override means the recruiter-facing thesis in the README screenshot (QNT-66) and the hero demo (QNT-94) use the high-quality model without the project's API surface or eval harness changing.
- **Eval harness becomes a provider-comparison artifact.** Logging per-provider columns in `evals/history.csv` (QNT-67) turns the Groq↔Claude split into a measurable AI-engineering signal rather than an invisible default.

**Harder:**

- **Two API keys to manage** (`GROQ_API_KEY`, `ANTHROPIC_API_KEY`) instead of one. SOPS already handles prod (QNT-102); dev `.env.example` gets a second line.
- **Groq model catalog churns.** Groq rotates which models they host; `llama-3.3-70b-versatile` today could be deprecated in six months and replaced. Mitigation: the `equity-agent/default` alias in `litellm_config.yaml` is the one place to update — agent code is unaffected.
- **Free-tier rate limits are a real ceiling for bursty backfills.** 30 RPM means an eval harness running 20 questions back-to-back needs ~40 s; a 100-question batch would rate-limit. Upgrade path is Groq Developer tier (10× limits, $25/mo free credit, credit card required) — re-evaluate when/if we hit the ceiling.
- **Groq region + data residency.** Free tier runs in their US region. Acceptable for public market data; flag if project ever handles anything private.
- **Quality ceiling below frontier models.** llama-3.3-70b is competitive but not the top of any leaderboard. Anything demanding frontier reasoning (the hero thesis, the golden-set reference answers) should explicitly route to the Claude override. ADR-003's "interpret, don't calculate" mandate plus QNT-67's hallucination eval reduce the pressure on raw model quality, but don't eliminate it.

## Revisit triggers

Reopen this ADR if any of these fire:

- Groq free-tier RPD ceiling hit during normal Phase 5 dev (bump to Developer tier or swap default to Gemini Flash).
- llama-3.3-70b-versatile deprecated from Groq without a clear successor.
- QNT-67 eval harness shows a qualitatively different thesis shape on Claude vs Groq large enough that a Groq default produces unrepresentative demo artifacts.
- Project scope expands to multi-turn conversational agents where the in-memory LangGraph checkpointer (implicit in the current design) stops being enough — re-examine provider choice alongside checkpointer persistence.
