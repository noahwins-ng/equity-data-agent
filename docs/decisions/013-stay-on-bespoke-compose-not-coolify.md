# ADR-013: Stay on Bespoke Compose + GitHub Actions, Reject Coolify (and Sibling PaaS)

**Date**: 2026-04-19
**Status**: Accepted

## Context

After ~2 weeks of reactive Ops & Reliability work (QNT-88 through QNT-96 — CD SHA-drift gates, restart policy, kernel-reboot alerting), it became visible that several of the gates we'd hand-rolled were compensating for defaults a self-hosted PaaS like [Coolify](https://coolify.io) would ship for free:

- `restart: unless-stopped` (we had to add it after the Apr-18 reboot outage)
- HEALTHCHECK + log rotation + `mem_limit` (QNT-100)
- Container-state notifications (QNT-101)
- Logs UI + resource metrics (QNT-103)
- Push-button rollback dashboards
- Optional preview environments for the frontend

Two tickets were filed to pursue this:
- **QNT-97** — spike to evaluate Coolify
- **QNT-98** — bootstrap Coolify on the existing Hetzner CX41 alongside the running compose stack

Both were cancelled within ~24 hours of being filed. This ADR captures **why we abandoned the Coolify direction and committed to keep the bespoke compose + GitHub Actions stack**, so future-me (or a recruiter reading the repo) doesn't re-litigate it.

## Decision

**Stay on the existing topology**: docker-compose on a single Hetzner CX41, GitHub Actions as the CD entry point, frontend on Vercel. Address the configuration gaps that motivated the Coolify look directly, as targeted compose-layer tickets:

| Gap motivating Coolify | Resolved by |
|---|---|
| No runbook to grep at 3am | **QNT-99** — ops runbook skeleton |
| Sick-but-still-up containers | **QNT-100** — HEALTHCHECK + log rotation + `mem_limit` |
| `/health` failures rot in a log file | **QNT-101** — UptimeRobot + docker-events Discord notifier |
| Plaintext `.env` on VPS | **QNT-102** — SOPS-encrypted secrets |
| No logs UI / metrics | **QNT-103** (Phase 7) — Dozzle + Prometheus/Grafana/cAdvisor |
| Preview envs for frontend | **Vercel** (already in scope per ADR-005) |

Each gap gets a concrete compose-layer fix instead of a tool that papers over them.

## Alternatives Considered

### 1. Adopt Coolify (rejected — the original temptation)
Self-hosted, OSS (Apache 2.0), reasonable PaaS UX. Would have given us the table above as defaults plus a deploy/rollback dashboard.

**Why rejected**:
- **Adds critical infra of its own.** Coolify becomes a single point of failure for *every* deploy and runtime decision, with its own DB (Postgres) and queue (Redis) we'd then have to monitor, back up, and recover. We'd be trading "compose drift" risk for "Coolify-the-control-plane drift" risk on a single VPS.
- **Doesn't model Dagster's shape cleanly.** Dagster needs the production topology in ADR-010 (code-server + daemon + webserver + DockerRunLauncher run-workers as ephemeral containers, plus a workspace.yaml bind-mount, plus `/var/run/docker.sock` access for the daemon). Coolify's compose support is fine for stateless web apps; once we want per-service `mem_limit`, healthchecks tied to `dagster api grpc-health-check`, and a launcher that spawns sibling containers via the Docker socket, we'd be writing Coolify-flavored YAML to express what plain compose already expresses idiomatically. The mismatch is highest exactly where our reliability work lives.
- **Erodes direct-to-metal muscle.** The Ops & Reliability work isn't only about closing gaps — each incident (Apr-16 SHA drift, Apr-18 reboot, Apr-20/21 OOM cascade) has been load-bearing learning. Hiding the same surfaces behind a PaaS dashboard would make that learning shallower without making prod meaningfully more reliable in the next few months.
- **Sunk-cost is small but real.** CD already deploys reliably; the hard gates from QNT-88/89/90 already prove "deploy green = code deployed". Switching control planes for incremental polish has migration cost (downtime risk on the cutover, revisiting every gate) without a corresponding incident it would have prevented.
- **Frontend on Vercel kills the strongest argument.** Preview environments — one of Coolify's bigger wins — are owned by Vercel for the only thing that wants them (Next.js). Backend is batch infra: yfinance pulls, weekly fundamentals, news embeddings. A deploy dashboard is low-value on workloads that aren't user-facing.

### 2. Dokploy / CapRover / Portainer
Same shape as Coolify — different vendor, same structural objections. Portainer specifically is closer to a compose UI than a PaaS, so it's a smaller bet but also a smaller payoff; the gaps that motivated this ADR are the *config* defaults, not the *visibility*.

### 3. Move to Hetzner-managed Kubernetes / DigitalOcean App Platform / Fly.io
Materially different infra. Out of scope for a 10-ticker portfolio project on a single CX41. The "going to production" page for any of these is more pages than the entire current ops-runbook; revisiting only if we outgrow the single-VPS topology (one of ADR-010's revisit triggers).

### 4. Stay on bespoke compose + close the gaps directly (chosen)
Concrete tickets land each missing default. Lower lock-in, lower control-plane risk, retains the direct-to-metal exposure that has been generating durable lessons. Cost is ours to absorb: every PaaS-as-a-default is now a small ticket on the Ops & Reliability board.

## Consequences

### Easier
- **No new control plane** to learn, monitor, back up, or recover. Compose is the contract.
- **Dagster's production topology (ADR-010) ports cleanly.** workspace.yaml + DockerRunLauncher + bind-mounts work natively with plain `docker compose up -d`; no Coolify-shaped translation layer.
- **The repo stays the source of truth for runtime state.** No "click in Coolify to enable X" drift between repo and prod (the same drift class as QNT-92 sensor `default_status` and QNT-112 named-volume shadow). Everything that runs in prod is in `docker-compose.yml`, `dagster.yaml`, `workspace.yaml`, or a `.github/workflows/*.yml`.
- **Each Ops & Reliability lesson stays visible.** QNT-100/101/102/103 read as documented decisions in the repo, not "Coolify defaults this".

### Harder
- **No deploy/rollback dashboard.** Rollback is `make rollback` on the VPS, not a button. Acceptable for a single operator on a 10-ticker stack.
- **No logs UI / metrics dashboard until Phase 7.** Mitigated by `docker compose logs`, the docker-events-notify Discord channel (QNT-101), and `make monitor-log` for health-check history. Real Dozzle + Grafana lands in QNT-103.
- **Every PaaS default is now a ticket.** This is the trade we're explicitly accepting; we get the lesson in exchange.
- **Closes the door on cheap preview environments for the backend.** No real demand for them — backend is batch infra with no per-PR demo surface — but worth naming.

## Revisit Triggers

Reconsider this ADR if **any** of:

1. We outgrow the single-VPS topology (also a revisit trigger for ADR-010).
2. A second operator joins and the "no dashboard" tax becomes real on something other than read-only inspection.
3. The Ops & Reliability ticket queue becomes a permanent treadmill — i.e. we're shipping the same compose-default fix on a third unrelated service.
4. Dagster's deployment story shifts toward something a PaaS expresses more naturally than raw compose.
