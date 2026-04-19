# Ops Runbook

A living catalog of production failure modes and their response procedures. When prod breaks, grep this file first.

## How to use

1. Search by symptom (e.g., `/health 503`, `Exited (0)`, stale deploy).
2. Run the **Diagnosis** commands to confirm the failure class.
3. Execute **Response** steps.
4. Check **Prevention** — if a detector already catches this, great; if a gap remains, file a ticket.

## How to update this runbook

Every Ops & Reliability ticket is expected to add a new entry (or extend an existing one) as part of its acceptance criteria. Use the template below. Entries are rooted in specific observed or simulated failures — do not pre-populate speculative ones.

### Section template

```markdown
### <Short failure name>

**Symptoms**: what you observe (logs, health status, user impact)
**Diagnosis**: exact commands to confirm this is the failure
**Response**: step-by-step remediation
**Prevention**: what detector/config prevents this (link to QNT-XX)
**Last occurred**: YYYY-MM-DD (or "not yet occurred — preventative")
```

---

## Failure modes

### Stale deploy (CD green, prod running old code)

**Symptoms**:

- CD workflow reports green.
- `/health` returns 200.
- Behavior on prod does not match recently merged changes (missing endpoints, old UI, absent features).
- `ssh hetzner "cd /opt/equity-data-agent && git log --oneline -1"` lags behind `origin/main`.

**Diagnosis**:

```bash
# Compare prod SHA to latest merged commit
ssh hetzner "cd /opt/equity-data-agent && git rev-parse HEAD"
gh api repos/noahwins-ng/equity-data-agent/commits/main --jq .sha

# Check the running container's image age
ssh hetzner "docker inspect equity-data-agent-api-1 --format '{{.Image}} {{.Created}}'"
```

**Response**:

1. SSH to Hetzner and reconcile the tree with `origin/main`:
   ```bash
   ssh hetzner "cd /opt/equity-data-agent && git fetch origin main && git reset --hard origin/main"
   ```
2. Rebuild without cache and bring services up:
   ```bash
   ssh hetzner "cd /opt/equity-data-agent && docker compose --profile prod build --no-cache && docker compose --profile prod up -d"
   ```
3. Verify post-deploy SHA equals the merged commit.
4. If someone has `scp`'d files to prod directly (`git status` on prod shows local mods), discard them first, then pull — see `feedback_prod_hotfix_scp.md`.

**Prevention**:

- [QNT-88](https://linear.app/noahwins/issue/QNT-88) — CD asserts `git rev-parse HEAD` on prod equals `github.sha` after deploy; workflow goes red if they diverge.
- Never `scp` files to prod — always repo → branch → PR → CD.

**Last occurred**: 2026-04-16

---

### Host reboot outage (containers exit cleanly, never restart)

**Symptoms**:

- API unreachable; `/health` fails or returns 503.
- `docker compose --profile prod ps` shows services in `Exited (0)` state.
- Uptime loss correlates with Hetzner VM maintenance, kernel update, or VPS reboot.

**Diagnosis**:

```bash
make check-prod                                                                 # expect: all services Up, /health 200
ssh hetzner "uptime"                                                            # check how long host has been up
ssh hetzner "docker inspect equity-data-agent-api-1 --format '{{.HostConfig.RestartPolicy.Name}}'"
# expect: unless-stopped
make monitor-log                                                                # see when health-monitor first failed
```

**Response**:

1. Bring services back:
   ```bash
   ssh hetzner "cd /opt/equity-data-agent && docker compose --profile prod up -d"
   ```
2. Wait ~30s, then `make check-prod` to confirm `/health` returns 200.
3. If any prod service's `RestartPolicy.Name` is not `unless-stopped`, deploy the current `main` (QNT-95 applies it to every prod service).

**Prevention**:

- [QNT-95](https://linear.app/noahwins/issue/QNT-95) — `restart: unless-stopped` on every prod-profile service so the container daemon revives them after a host reboot.
- Health-monitor cron (`make monitor-install`) catches extended outages within 15 min.

**Last occurred**: 2026-04-18
