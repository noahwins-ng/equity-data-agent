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

---

### API down / 503

**Symptoms**:

- Uptime monitor alert (UptimeRobot / BetterStack / uptime-kuma) fires on `/api/v1/health`.
- Direct probe: `curl -sf http://<prod-host>:8000/api/v1/health` returns 503 or times out.
- User-visible: frontend/agent requests fail with 500/502/504 or hang.

**Diagnosis**:

```bash
# Does the endpoint still respond, and what does it say is down?
ssh hetzner 'curl -sS http://localhost:8000/api/v1/health; echo'
# Expect JSON like {"status":"down","services":{"clickhouse":"down",...}} — the
# payload names the failing dependency. 503 = ClickHouse unreachable; 200 with
# status=degraded = Qdrant down (not alerting-worthy until Phase 4 matures).

make check-prod                                 # all services Up?
ssh hetzner "docker compose --profile prod logs api --tail=50"
ssh hetzner 'docker inspect equity-data-agent-api-1 --format "{{.State.Health.Status}}"'
```

**Response**:

1. If `services.clickhouse == "down"`: jump to "Host reboot outage" if containers are `Exited`, or to "Container wedged but still up" if `clickhouse` is `Up (unhealthy)`.
2. If the API container itself is wedged (`health.Status` is `unhealthy` but `State.Status` is `running`): `ssh hetzner "docker restart equity-data-agent-api-1"` and confirm `/api/v1/health` returns 200 within 30 s.
3. If the API container is crash-looping: see "Container crash loop" below — the underlying cause is almost always in the logs from the most recent exit.
4. Post-recovery: check the uptime-monitor incident timeline against the Discord `[DIE]`/`[OOM KILL]` messages — the two should line up on the same window, and together tell you what failed and why.

**Prevention**:

- [QNT-100](https://linear.app/noahwins/issue/QNT-100) — compose-level HEALTHCHECKs make wedged-but-running detectable (the `/health` endpoint can itself wedge).
- [QNT-101](https://linear.app/noahwins/issue/QNT-101) — external uptime probe (UptimeRobot or equivalent) catches the case where the host is unreachable and local health monitoring can't help.

**Last occurred**: not yet occurred — preventative

---

### Container crash loop

**Symptoms**:

- Repeated Discord `[DIE]` or `[RESTART]` notifications for the same container name within minutes (`restart: unless-stopped` keeps relaunching it).
- `ssh hetzner "docker ps"` shows the container in `Restarting` state, or uptime is <1 min on every inspection.
- Possibly also an uptime alert if the crashing service is API/ClickHouse.

**Diagnosis**:

```bash
# Exit code + last-state reason
ssh hetzner 'docker inspect equity-data-agent-<service>-1 --format "exit={{.State.ExitCode}} oom={{.State.OOMKilled}} error={{.State.Error}}"'

# Logs from the dying process — --tail 200 usually captures the stack trace
ssh hetzner "docker compose --profile prod logs <service> --tail=200"

# If OOMKilled=true, check resource pressure pre-crash
ssh hetzner 'docker stats --no-stream'
```

**Response**:

1. **Stop the loop first.** A crash-looping container burns CPU and log volume.
   ```bash
   ssh hetzner "docker stop equity-data-agent-<service>-1"
   ```
2. Root-cause by exit-code class:
   - `exit=137` + `OOMKilled=true` → memory limit too tight (compare `mem_limit` in `docker-compose.yml` to the process's working set). If tight: raise the limit **or** revert the commit that introduced the regression.
   - `exit=139` (SIGSEGV) → native-extension crash, almost always a bad image. Roll back: `make rollback`.
   - `exit=1` or `exit=2` → Python unhandled exception; logs name the traceback. Fix in code, ship a new commit.
   - `exit=0` + repeated restarts → process exits cleanly because its main loop terminated (config error, failed healthcheck dependency). Check the service's `command` / entrypoint args.
3. After a code fix, re-deploy via normal CD. Do **not** `scp` a patched file to prod — see `feedback_prod_hotfix_scp.md`.
4. Restart only when you've named the root cause: `ssh hetzner "docker compose --profile prod up -d <service>"`.

**Prevention**:

- [QNT-101](https://linear.app/noahwins/issue/QNT-101) — this ticket: container-state notifier surfaces the crash within 30 s rather than waiting for the 15-min health-monitor cron.
- [QNT-100](https://linear.app/noahwins/issue/QNT-100) — resource limits keep a leaking container from starving its neighbours; log rotation prevents crash-loop logs from filling the disk.
- [QNT-104](https://linear.app/noahwins/issue/QNT-104) *(pending)* — autoheal sidecar for the wedged-but-not-crashed case.

**Last occurred**: not yet occurred — preventative

---

### Deploy-noise alerts suppressed too long (stuck sentinel)

**Symptoms**:

- Real container failure on prod (crash, OOM, manual `docker kill`), but no Discord `[DIE]`/`[KILL]`/`[OOM KILL]` alert fires.
- `ssh hetzner 'ls -la /opt/equity-data-agent/.deploy-in-progress'` shows the sentinel exists.
- No CD workflow has run recently (check `gh run list --workflow=deploy.yml --limit 3`).

**Diagnosis**:

```bash
# Is the sentinel present and how old is it?
ssh hetzner 'ls -la /opt/equity-data-agent/.deploy-in-progress && date'

# Check last CD run — the sentinel should be removed by the "Close deploy window" step
gh run list --workflow=deploy.yml --limit 3

# Is the notifier still running?
make events-notify-status
```

**Response**:

1. If the sentinel is older than 10 minutes, the notifier's stale-sentinel fail-open logic already ignores it — alerts should be firing on new events. If they aren't, the issue is elsewhere (notifier down, Discord webhook rotated). Check `make events-notify-status`.
2. If the sentinel is fresh (<10 min) but no CD is running, a prior CD crashed mid-run and left it behind. Clear it manually:
   ```bash
   ssh hetzner 'rm -f /opt/equity-data-agent/.deploy-in-progress'
   ```
3. Verify recovery with a fake event: `make events-notify-test` should fire a Discord alert within ~30 s.
4. Inspect the failed CD run (`gh run view <run-id>`) to understand why the cleanup step didn't run, and whether it's a recurring failure mode.

**Prevention**:

- [QNT-109](https://linear.app/noahwins/issue/QNT-109) — sentinel mechanism uses fail-open (>10 min = ignored) so a stuck sentinel can only mute alerts for 10 minutes max, not forever. The `if: always()` cleanup step in CD also runs on failed deploys.

**Last occurred**: not yet occurred — preventative

---

### Container wedged but still "up" (healthcheck unhealthy, no crash)

**Symptoms**:

- `docker compose ps` or `make check-prod` shows the container `Up`, but users see persistent HTTP 500s, stale data, or extremely slow responses.
- `/health` (API) may return 503 or time out, while the container process stays alive.
- No OOM kill, no container exit — the process is running but wedged (deadlock, blocked on I/O, exhausted worker pool).

**Diagnosis**:

```bash
# Check healthcheck status — expect "healthy"; wedged containers report "unhealthy"
ssh hetzner 'docker inspect equity-data-agent-api-1 --format "{{.State.Health.Status}}"'

# Last few healthcheck probes (shows what the check actually returned)
ssh hetzner 'docker inspect equity-data-agent-api-1 --format "{{json .State.Health.Log}}" | jq .'

# Resource pressure — near-limit memory or CPU suggests leaking / saturated service
ssh hetzner 'docker stats --no-stream equity-data-agent-api-1'
```

**Response**:

1. Manual restart to clear the wedged state:
   ```bash
   ssh hetzner 'docker restart equity-data-agent-<service>-1'
   ```
2. Wait ~30s, then `make check-prod` to confirm recovery.
3. Inspect logs for the window before the wedge to find the root cause:
   ```bash
   ssh hetzner 'docker compose -f /opt/equity-data-agent/docker-compose.yml logs <service> --since 10m --tail 200'
   ```

**Prevention**:

- [QNT-100](https://linear.app/noahwins/issue/QNT-100) — compose-level HEALTHCHECK on every service surfaces the wedged state in `docker inspect` + `docker compose ps` + log UIs. Detection only; manual restart required.
- [QNT-104](https://linear.app/noahwins/issue/QNT-104) *(pending)* — adds an `autoheal` sidecar that watches healthcheck status and kills unhealthy containers so `restart: unless-stopped` picks them up. Auto-recovery within ~90s of going unhealthy.

**Last occurred**: not yet occurred — preventative
