# ADR-017: Public chat — truly public, no auth, defense-in-depth via rate limiting

**Date**: 2026-05-02
**Status**: Accepted

## Context

QNT-75 makes the chat panel reachable to anyone who lands on the deployed Vercel frontend. The panel hits `POST /api/v1/agent/chat` on the Hetzner FastAPI directly via `NEXT_PUBLIC_API_URL`. Pre-QNT-161, that endpoint had:

- No request rate limit.
- No auth.
- No per-IP cost cap.
- A 4000-char message length cap (QNT-156) — the only existing guard.
- CORS open to any `*.vercel.app` origin.

Once the URL leaks (and it will — search engines crawl Vercel previews; people share links), the endpoint becomes an availability-bomb. The agent runs LLM calls via LiteLLM to Groq's free tier; Groq's daily token quota (~100K TPD on `llama-3.3-70b-versatile`) is the binding constraint, not a dollar amount. A scraper that exhausts the quota = the agent stops working = every subsequent recruiter sees a broken demo until quota reset.

**Reframed threat model:** the risk is NOT a runaway monthly bill (free tier). The risk IS demo uptime. We need controls tight enough that abuse can't take the demo dark, while keeping the panel frictionless for the audience it exists for.

## Decision

**Auth model: option (a) — truly public.** No API key, no password, no signup, no "request access" form. Defense-in-depth via:

1. **Per-IP request rate limit** (SlowAPI): `5/minute, 30/hour, 100/day`. Returns 429 with `Retry-After`.
2. **Per-IP daily token budget**: ~10K Groq tokens / IP / day. On exhaustion, the SSE stream emits a deterministic conversational redirect ("daily demo limit reached; fork the repo to run locally") instead of invoking the LLM.
3. **Global daily Groq TPD circuit breaker**: ~50% of the active model's TPD ceiling. Once tripped, every new request gets the friendly redirect for the rest of the day, regardless of IP. Defends against rotating-IP attacks AND the long-tail "many IPs each just under the per-IP cap" pattern.
4. **CORS allowlist locked** to the explicit Vercel project domain + project-pinned preview regex + localhost for dev. No more wildcard `*.vercel.app`.
5. **Prompt-injection input filter**: rejects control characters (other than `\n` / `\t`) and overlong identifier tokens (>500 chars).
6. **Sentry alert** on burst patterns (one IP exceeding N × 429s in 5 min) and on global-breaker trip.
7. **FAIL CLOSED** — the LiteLLM fallback chain is audited (`tests/agent/test_litellm_fail_closed.py`) to contain only Groq + Gemini free-tier providers. A Groq quota event NEVER falls through to a paid provider.

The frontend chat panel surfaces a small `demo: ~30 queries/hour per visitor; powered by Groq free tier` hint so the cap is set as an expectation BEFORE the user hits it. On 429 / token-budget redirect, the conversational card explains why and points to the repo for local-run instructions.

## Why no auth

This repo is a portfolio piece. The chat panel must stay reachable to recruiters and hiring managers without friction — a click on the link, one or two questions, decision in 30 seconds. Any auth wall (API key, password protection, signup flow, "join our waitlist") destroys that flow:

- **API key** — recruiters won't request, receive, paste, and configure a key for a 30-second eval. Bounce.
- **Password protection** (e.g. Vercel Edge Middleware) — same friction, plus the password ends up in a screenshot or shared link within hours.
- **Signup flow** — adds a database (Postgres + auth provider), email verification, a privacy policy. Wrong scope for a portfolio project; the auth surface itself becomes a maintenance burden bigger than the agent it gates.
- **GitHub OAuth** — friction lower than email signup, but still > 0 clicks. And many recruiters / hiring managers use GitHub accounts they don't want associated with portfolio reviews.

The defense model therefore has to be **defense-in-depth via rate limiting + token budgets + circuit breaker**, not auth gates. The cost of "an abuser gets through" is bounded — the agent stops responding, the redirect copy shows up. Worst case: the demo serves a friendly redirect for a few hours until the daily quota resets. That is a much smaller failure than the cost of every recruiter bouncing because the panel asked for credentials.

## Alternatives Considered

### (b) Lightweight API key

A single hardcoded API key, distributed in the README / linked from the landing page, baked into the frontend via `NEXT_PUBLIC_API_KEY`. Provides the illusion of a gate but the secret is in every page-source view.

- Pro: trivially blocks naive crawlers.
- Con: a determined scraper extracts it from the bundle in seconds. Adds a "things to remember to rotate" surface for zero actual security gain.
- Con: any auth wall, even a fake one, raises the recruiter friction question. If we add the surface, we should add real auth — but we already ruled that out above.
- Rejected as security theatre.

### (c) Cloudflare WAF / bot detection in front of the API

Cloudflare's free tier includes basic bot detection + WAF. Could front the Hetzner API with a Cloudflare proxy.

- Pro: real bot detection, would catch most scrapers before they hit our app code.
- Pro: would also handle DDoS at the Cloudflare edge.
- Con: introduces a new dependency in the request path; adds a new place where outages can happen (a Cloudflare incident takes the chat panel down even when our infra is fine).
- Con: bigger surface to debug — every "request didn't reach the API" issue now has a CF-edge layer to inspect.
- Con: the threat model is "demo uptime", not "data exfiltration"; rate limiting + token budgets are sufficient for the threat model we have.
- **Deferred.** Re-open if abuse incidents materialise post-deploy. The QNT-161 controls are designed so adding Cloudflare later is purely additive — no change to the agent or the FastAPI surface.

### (d) Per-user accounts + per-user budgets

A real auth system with per-user token budgets, billing-shaped controls, the works.

- Pro: most precise abuse model; a known abuser is single-user-bannable.
- Con: massive scope creep for a portfolio project. Would multiply the surface area of the codebase and the maintenance burden.
- Con: defeats the "click and try" recruiter flow, same problem as (b) and any auth-wall option.
- Rejected as wrong scope.

### (e) Disable the chat panel for unauthenticated traffic; show a static demo screenshot

The middle ground — recruiters see a screenshot of a real thesis, can't trigger one themselves.

- Pro: zero abuse surface; quota is never touched by external traffic.
- Con: defeats the whole point of the panel, which is to demo the *interactive* agent. A static screenshot proves we can take a screenshot, not that the agent works.
- Pro: would still be a valid fallback if option (a) ever proves untenable in practice.
- Rejected unless the rate-limit + budget controls prove insufficient.

## Consequences

**Easier:**

- **Recruiters can use the panel without friction.** Click → ask → see a thesis → close tab. The whole interaction is < 60 seconds, no credentials anywhere in the flow.
- **No auth surface to maintain.** No user table, no password reset flow, no OAuth provider integration, no session cookies, no account-deletion compliance.
- **The cost of compromise is bounded.** Worst case: someone exhausts the daily token quota. The demo shows a friendly redirect for the rest of the day. Everything resets at UTC midnight.
- **The fail-closed contract is proven by code.** `tests/agent/test_litellm_fail_closed.py` audits the LiteLLM config and asserts no paid provider is reachable from the chat path. A future contributor who adds an Anthropic alias as a fallback trips a CI failure, not a billing alert.

**Harder:**

- **No "who is abusing this" forensics.** All we know is an IP. If a residential IP rotates through CG-NAT, multiple users share one entry in our budget map; one heavy real user can starve another. Mitigation: the per-IP cap is sized for ~10–15 thesis runs / day, comfortably above any one recruiter's needs even shared across a household.
- **Burst alerts will fire on legitimate traffic occasionally.** A user who keeps hitting "send" through their own typo will trip the SlowAPI cap. The Sentry alert is sized to dedup (one alert per IP per 5-minute window) so this is noise, not an outage signal.
- **The 50% global-TPD reservation costs us iteration headroom.** The Groq Llama-3.3-70B free tier is 100K TPD; reserving 50K for the chat path leaves 50K for the daily ingest pipeline + the user's own dev usage. That's tight. If a heavy iteration day blows through the dev budget, the chat path keeps running but local agent calls start hitting the reserved cap.
- **A determined attacker can rotate IPs to evade the per-IP cap.** This is exactly what the global breaker exists to handle — once aggregate spend crosses the cap, every IP gets the redirect. So the rotation defeats per-IP scoping but not the underlying spend ceiling.
- **The "fork the repo" suggestion in the friendly redirect assumes the repo is public and runnable end-to-end.** It is, today (QNT-66 / README). If the project ever flips to private or the README run-instructions decay, the redirect copy starts pointing at a dead end. Add to the cycle-end retro checklist: "verify the repo is still clone-and-run if the chat panel is gated by friendly redirects."

## Revisit triggers

Reopen this ADR if any of these fire:

- Abuse incidents (sustained scraper traffic, repeated breaker trips) materialise post-deploy. Most likely escalation: add Cloudflare per (c).
- A real product use-case emerges (paid users, business customers) — option (d) becomes worth its weight.
- Groq's free-tier TPD ceiling drops below the level where a meaningful per-IP budget is possible. Most likely response: switch the default LLM provider per ADR-011's revisit triggers.
- The portfolio audience changes (e.g. project becomes a publication or course material) such that anonymous open-access stops being a goal — re-evaluate (b) / (e).

## References

- QNT-161 — implementation ticket.
- QNT-75 — Vercel deploy gated on this ADR landing.
- QNT-86 — Sentry integration; this ADR exposes alert hooks ahead of full wiring.
- ADR-011 — LiteLLM Groq + Gemini routing (the "free tier required" invariant this ADR builds on).
- `feedback_free_llm_providers.md` — project policy that drives Groq selection over paid Anthropic.
- `feedback_publish_only_on_clean_window.md` — Groq TPD bench discipline that informed the budget sizing.
- `tests/agent/test_litellm_fail_closed.py` — the executable form of the FAIL CLOSED contract.
