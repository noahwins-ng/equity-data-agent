# Teardown and Revival

Read this **before** tearing the project down, and again **first** if reviving it after
a long gap. It's the front door for that second case - it doesn't replace
`docs/guides/hetzner-bootstrap.md` (that's still the actual provisioning steps), it
tells you what's changed since that guide assumes continuous operation.

This is for a **one-time full teardown** (project no longer active, VPS destroyed to
stop paying for it), not a recurring pause/resume cycle. That's why there's no backup
automation here - a single manual checklist run once is the right amount of machinery
for a one-time event.

---

## Before tearing down

Run through this once, before deleting the Hetzner server.

### 1. Confirm the secrets escrow actually works

Prod secrets are SOPS-encrypted in git (`.env.sops`) and the age private key is
supposed to be escrowed independently of GitHub (Apple Notes / Bitwarden / 1Password -
see `hetzner-bootstrap.md` §11.1 step 7). Before tearing down, verify that escrow is
real, not just "I did this once months ago":

```bash
# From the escrowed copy, not your laptop's live key file:
sops --input-type dotenv --output-type dotenv -d .env.sops | head -3
```

If this fails, fix the escrow now - once the VPS is gone and the laptop's local key
file is your only copy, a lost laptop means every secret value is unrecoverable (see
`hetzner-bootstrap.md` §11.4).

### 2. Export the data that isn't cheaply re-derivable

Most of what's on the VPS is either disposable or trivially rebuildable:

| Data | Fate | Why |
|---|---|---|
| `dagster_home` (run history, schedule cursors) | **Disposable** - don't back up | Pure operational metadata; a fresh Dagster instance regenerates it as backfills run |
| `grafana_data`, `prometheus_data` | **Disposable** - don't back up | Dashboards are config, already reproducible; 15d metrics history has no value once the host is gone |
| `agent_memory` (chat session SQLite) | **Disposable** - don't back up | Session-scoped follow-up memory, not a data asset |
| ClickHouse `equity_derived` (RSI, indicators, etc.) | **Disposable** - don't back up | Pure function of `equity_raw`; recomputes on backfill |
| ClickHouse `equity_raw` price/fundamentals | **Cheap to re-derive** - skip backup | Vendor (yfinance etc.) keeps full history; re-backfill on revival |
| ClickHouse `equity_raw.news_raw` article text | **Export this** | News APIs have retention/rate windows - the exact articles ingested months or years ago may not be re-fetchable later. This is the one table where "just re-backfill" doesn't hold. |

Export the news table before teardown, keep it somewhere durable outside git (it's raw
scraped text, not code - a personal cloud drive or external disk is fine):

```bash
ssh hetzner "docker compose exec -T clickhouse clickhouse-client \
  --query 'SELECT * FROM equity_raw.news_raw FORMAT Native'" > news_backup_$(date +%F).native
```

(Table name as of this writing - if the schema has moved on by teardown time, check
`mcp__clickhouse__list_tables` or `docs/architecture/` for the current name.)

### 3. Decide the fate of external, separately-billed services

These live outside Hetzner and survive a VPS teardown untouched - but some of them cost
money independently. Decide per-service whether to cancel or leave running:

- **Qdrant Cloud** (news embeddings) - if you delete the cluster, note that its
  contents are cheaply regenerable *from the news export above* (re-embedding is a
  pure function of stored article text), not from re-scraping.
- **Langfuse**, **Sentry** - free-tier SaaS; likely fine to just let sit idle, but
  check if either auto-suspends a dormant project.
- **Domain / DNS / Cloudflare tunnel** - if the domain registration or Cloudflare
  zone is going away too, note that the named tunnel and DNS record will need to be
  recreated from scratch on revival (see `hetzner-bootstrap.md` §5).
- **GitHub repo, Vercel project** - free tier, no action needed; both are naturally
  dormant when nothing pushes to them.

### 4. Tear down

Delete the Hetzner server. Everything else (git repo, `.env.sops`, the escrowed age
key, the news export) is what revival will be built from.

---

## On revival

The bootstrap guide (`docs/guides/hetzner-bootstrap.md`) is still the actual runbook -
follow it end to end. But it was written assuming the gap between "write it" and
"follow it" is days, not months or years. Specifically re-check these before trusting
it blindly:

- **OS version** - the guide specifies Ubuntu 22.04. Check it's still supported;
  if not, use current LTS and expect minor package-manager differences.
- **Docker install script** - `curl -fsSL https://get.docker.com | sh` is a moving
  target maintained by Docker upstream, not pinned. Sanity-check `docker --version`
  and `docker compose version` after install match what the compose file expects.
- **Lockfiles are stale on purpose - don't blindly bulk-upgrade.** `uv.lock` and the
  frontend lockfile are exactly as they were at teardown. On revival there will be a
  large Dependabot-shaped backlog on the first CI run. Classify each bump's severity
  by the *locked* version delta (what `uv.lock`/the frontend lockfile actually pins),
  not the `pyproject.toml`/`package.json` constraint floor - a lockfile that already
  sat one major version ahead of its floor isn't a major bump when Dependabot proposes
  the next patch. Don't `--upgrade-package` everything at once.
- **GitHub Actions secrets need the new server's identity.** `HETZNER_HOST` will be
  a new IP; re-run `gh secret set HETZNER_HOST` etc. `SOPS_AGE_KEY` does **not**
  need to change - it's tied to the escrowed age keypair, not the server.
- **Cloudflare tunnel + DNS** must be recreated for the new server if they were torn
  down in step 3 above - this is manual dashboard work, not code (ADR-018).
- **Qdrant Cloud** - if the cluster was deleted, recreate it and re-embed from the
  news export (`news_backup_*.native`) rather than re-scraping from source.
- **ClickHouse data** - run migrations (`make migrate`), then re-run Dagster
  backfills for `equity_raw` price/fundamentals (full vendor history is available
  anytime) and restore the news export for anything not re-fetchable, then let
  `equity_derived` recompute from there.

## References

- `docs/guides/hetzner-bootstrap.md` - the actual from-scratch provisioning steps
- `docs/guides/ops-runbook.md` - failure-mode catalog for a *running* prod, not
  relevant until the stack is back up
- ADR-013 - why this project stays on bespoke compose instead of a PaaS (relevant
  context for why revival is a manual runbook, not a one-command script)
- ADR-018 - Cloudflare named tunnel for HTTPS ingress
