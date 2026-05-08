# ADR-018: Cloudflare Quick Tunnel for HTTPS Ingress (No Custom Domain)

**Date**: 2026-05-02
**Status**: Superseded 2026-05-08 by named-tunnel migration (QNT-177) — see Update at end.

## Context

QNT-75 (Vercel deploy) needs the Hetzner FastAPI to be reachable over HTTPS so the Vercel-hosted Next.js frontend can call it without browser mixed-content blocking. The straightforward path — buy a domain, point DNS at the Hetzner IP, terminate TLS via Caddy + Let's Encrypt — was scoped in the original Phase 6 plan and remains supported in `Caddyfile` and `docker-compose.yml`.

But the project is a portfolio piece, not a revenue product. Spending money on a domain (~$10/yr) and managing DNS adds friction for a target audience (recruiters, hiring managers) that won't notice the URL. The defenses for the public chat endpoint already shipped with QNT-161 — rate limiting, per-IP token budget, global Groq TPD circuit breaker, prompt-injection input filter, CORS allowlist — and don't depend on the ingress path.

The remaining question was: how do we get HTTPS to the FastAPI without buying a domain?

## Decision

Use **Cloudflare's quick-tunnel mode** (`cloudflared tunnel --url ...`) to expose the FastAPI service at a free `*.trycloudflare.com` HTTPS hostname. The Vercel frontend reads that hostname via `NEXT_PUBLIC_API_URL`. The Hetzner host firewalls FastAPI to loopback only — no public ingress port at all. Public traffic flows:

```
Browser  ──HTTPS──>  https://your-app.vercel.app  (Vercel CDN, frontend)
Browser  ──HTTPS──>  https://xxx.trycloudflare.com/api/v1/...
                              │
                              ▼ (Cloudflare edge — WAF, DDoS protection, free)
                     cloudflared tunnel (outbound from Hetzner — no inbound port)
                              │
                              ▼ (Docker bridge network)
                     api:8000 (FastAPI, loopback-bound on host)
```

The single trade-off the team accepts is that quick-tunnel hostnames are **ephemeral** — every restart of `cloudflared` produces a new `*.trycloudflare.com` URL. With `restart: unless-stopped` and Hetzner's stable uptime, rotation in practice happens 1-2 times a year (kernel reboots, container crashes). When it rotates, recovery is ~5 minutes: read the new URL from `docker logs cloudflared`, paste into the Vercel project's `NEXT_PUBLIC_API_URL`, redeploy. Documented in `docs/guides/vercel-deploy.md`.

## Alternatives Considered

**A. Buy a domain + Caddy on Hetzner (the original Phase 6 plan)**

- TLS terminates at Caddy, port 443 publicly exposed.
- Pro: stable URL, no rotation maintenance.
- Con: ~$10/yr ongoing cost, DNS management, certificate renewal monitoring (Caddy automates renewal but adds a failure mode).
- Rejected: cost-without-benefit for a portfolio piece. The Caddyfile and docker-compose `caddy` service are retained under the `prod-caddy` profile so this path can be activated later without code changes — register a domain, edit `Caddyfile`, run `docker compose --profile prod-caddy up -d`.

**B. Cloudflare named tunnel + Cloudflare-registered domain**

- Same security profile as quick tunnel, but stable URL via `api.<your-domain>`.
- Pro: stable URL, integrates with full Cloudflare account features (Access policies, custom WAF rules).
- Con: still needs ~$8-10/yr for the domain (Cloudflare sells at near-cost but it's not free).
- Rejected: same cost objection as A, with the added complexity of Cloudflare account setup and DNS configuration.

**C. Vercel rewrite to plain `http://<hetzner-ip>:8000`**

- Vercel's edge proxies `/api/*` to a plaintext Hetzner endpoint server-side — browser only sees HTTPS, so no mixed-content block.
- Pro: zero infra changes, $0.
- Con: plaintext on the Vercel→Hetzner leg (low-risk for public-only data but a real downgrade). FastAPI port :8000 stays publicly exposed, allowing direct bypass of the per-IP rate limit via `X-Forwarded-For` spoofing (only the global Groq TPD breaker still defends).
- Rejected: trycloudflare gives strictly better security at the same $0 cost. The only thing C wins on is "no maintenance when cloudflared restarts," and that maintenance is documented and infrequent.

**D. Cloudflare Tunnel via `*.trycloudflare.com` — chosen.**

## Consequences

**Easier:**
- End-to-end HTTPS for $0. Browser → Cloudflare → Hetzner (over the encrypted tunnel) is fully encrypted.
- Hetzner port :8000 closes to the public internet. The compose file binds it to `127.0.0.1` only; only `cloudflared` (on the same Docker network) can reach `api:8000`. Direct port-scan attacks against the Hetzner IP cannot reach the chat endpoint.
- Cloudflare's free-tier WAF and DDoS protection sit in front of the API automatically — better edge defenses than Caddy alone would provide.
- No domain registration, no DNS management, no certificate renewal.
- The QNT-161 abuse controls (rate limit, per-IP and global token budgets, prompt-injection filter) layer on top unchanged — they enforce on the request after Cloudflare has admitted it.

**Harder:**
- The trycloudflare URL rotates whenever `cloudflared` restarts. The Vercel env var must be refreshed manually each time. Recovery runbook in `docs/guides/vercel-deploy.md`.
- The trycloudflare URL is publicly discoverable (anyone with it can curl the API directly, bypassing Vercel's origin enforcement). CORS still gates browser requests by origin — only the Vercel domain is allowed — so XHR-from-other-pages remains blocked, but raw HTTP clients (curl, scripts) reach the chat endpoint without a browser-CORS check. The QNT-161 rate-limit + budgets still apply per IP, so direct-bypass abuse is bounded by the same defenses.
- Cloudflare's free quick-tunnel terms of service may change. The product has been stable for years but is not contractually guaranteed long-term. If Cloudflare retires or restricts the free quick-tunnel mode, the upgrade path is named tunnel + domain (alternative B above) or Caddy + domain (alternative A) — both pre-wired in the repo.
- Direct uptime probes against `<hetzner-ip>:8000` no longer work. UptimeRobot and similar must probe the trycloudflare URL or be retired in favor of the local `scripts/health-monitor.sh` cron (which uses `localhost:8000` from the host itself and continues to function).

## Upgrade Path

This decision is reversible. To switch to a custom domain at any future point:

1. Register a domain.
2. Either (a) update `Caddyfile` from `your-domain.com` to the registered name and start with `docker compose --profile prod-caddy up -d`, OR (b) install a named Cloudflare tunnel and CNAME `api.<domain>` to the tunnel UUID.
3. Re-expose `api:8000` to public (or to `127.0.0.1` only if going via Caddy on the same host) and adjust the `cloudflared` service or remove it.
4. Update `NEXT_PUBLIC_API_URL` in Vercel and redeploy.

No frontend code changes are required for either upgrade — the API client reads the URL from the env var.

## Update — 2026-05-08: Migration to Named Tunnel (QNT-177)

The cost objection in alternative B turned out to be moot — `nusaverde.com` was already on Cloudflare for an unrelated site, so the `api.<our-domain>` subdomain was free to claim with no incremental DNS or registration cost. Combined with the operational toll of the quick tunnel ("Harder" point #1: Vercel env var refresh on every cloudflared restart), the trade-off equation flipped.

**Trigger:** 2026-05-08 outage. The Hetzner host rebooted at 04:00 UTC after `unattended-upgrades` installed a kernel CVE patch. The cloudflared container restarted with `--url http://api:8000` and got a fresh `*.trycloudflare.com` hostname. The Vercel build (SHA `7591bfa`, baked the previous URL into `NEXT_PUBLIC_API_URL` ten hours earlier) was now stale; `generateStaticParams()` fetched the dead URL at build time, returned `[]`, and `dynamicParams = false` collapsed every `/ticker/*` slug to 404 until manual recovery. Same root pattern as the Apr-18 incident captured in memory (`feedback_health_endpoint_is_not_durability.md`); cadence is gated on Ubuntu kernel CVE releases (~3 weeks between events), not predictable.

**Decision:** Switch to alternative B — Cloudflare named tunnel anchored to `api.nusaverde.com`. The `cloudflared` service in `docker-compose.yml` now runs `tunnel --no-autoupdate run --token ${CLOUDFLARE_TUNNEL_TOKEN}`; the public hostname (`api.nusaverde.com` → `http://api:8000`) is configured in the Cloudflare Zero Trust dashboard. The connector token lives in `.env.sops` (CD decrypts and ships to Hetzner per QNT-102).

**Consequences of the switch:**

- The "Harder" point #1 above ("trycloudflare URL rotates whenever cloudflared restarts") no longer applies. Reboots, image bumps, and container recreates are all non-events for the public hostname.
- The "Harder" point #4 (uptime probes against the trycloudflare URL) is improved: probes can now point at `https://api.nusaverde.com/api/v1/health` permanently.
- The dormant `caddy` service, `Caddyfile`, and `prod-caddy` profile are removed in the same PR — the named-tunnel path was always one of the two upgrade options listed in the original ADR; we picked it, and the Caddy fallback is no longer load-bearing. Reverting to Caddy + own-domain (alternative A) is still possible by reading this ADR's history in git, but is not pre-wired in the working tree.
- `NEXT_PUBLIC_API_URL` is now `https://api.nusaverde.com` and is permanent — no more refresh-on-rotate runbook.

**Reversibility:** Switching back to a quick tunnel would require restoring the `--url` flag and removing the env-var requirement. Switching to Caddy + own-domain would require reintroducing the service definition + Caddyfile + opening firewall ports 80/443 + DNS A-record. Neither is expected; the named-tunnel mode supersedes the original choice.
