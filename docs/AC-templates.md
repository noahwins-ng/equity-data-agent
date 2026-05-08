# AC Templates

Reusable acceptance-criteria checklists to paste into Linear issues so common classes of change get verified consistently. `/sanity-check` and `/review` inspect the diff and apply the matching template as implicit AC when the issue description doesn't already include it.

## Infra / CI / Deploy PRs

Apply when `git diff --name-only main...HEAD` touches any of:

- `docker-compose.yml`
- `Dockerfile` (any)
- `.github/workflows/*.yml`
- `Makefile`
- Root config: `dagster.yaml`, `litellm_config.yaml`
- `scripts/*.sh`

### Default AC

- [ ] **CD runs green end-to-end** — all steps in `deploy.yml` pass, including the `Verify prod SHA matches merged commit` and `Verify Dagster loaded expected asset graph` gates (QNT-88 + QNT-89).
- [ ] **No prod drift** — `ssh hetzner 'cd /opt/equity-data-agent && git status --short'` returns empty output after deploy. Untracked runtime logs (`health-monitor.log`, `health-monitor-heartbeat`) are expected; anything else means SCP drift or uncommitted hotfixes.
- [ ] **Post-deploy smoke** — one cheap asset materialization succeeds on prod. Example:
  ```bash
  ssh hetzner 'docker exec equity-data-agent-dagster-daemon-1 \
    /app/.venv/bin/dagster asset materialize \
    --select ohlcv_raw --partition AAPL \
    -m dagster_pipelines.definitions 2>&1 | tail -5'
  ```
  Any success output is sufficient; the goal is to prove the runtime can actually execute, not to backfill data.

### How `/sanity-check` and `/review` use this

- If the branch diff touches any path in the trigger list, these three items are treated as required execution AC for the PR, regardless of whether the Linear issue author wrote them.
- The items classify as `[prod execution AC]` — they carry forward through `/sanity-check` and become `/ship` hard gates (see `ship.md` Step 7 "Hard gates").
- If the issue description already contains equivalent AC, don't duplicate; just mark them as satisfying this template.

### When NOT to apply

- Pure documentation PRs (`docs/`, `README.md` only).
- Comment-only changes to trigger-list files (no functional effect) — mark the PR "cosmetic, no deploy impact" to opt out.
- PRs that only touch `scripts/` files that are not invoked by CD or the prod host.

## Why this exists

During the Apr 16 2026 outage retrospective, we found that PRs touching `docker-compose.yml` or `deploy.yml` often don't carry runtime-verification AC — the Linear issue author is focused on the code change, and the "does it actually work in prod" step gets skipped. Three silent-deploy failures (Apr 14, Apr 16 ×2) made it through CD with green checks on stale code. These default AC close that gap mechanically instead of relying on reviewer vigilance.
