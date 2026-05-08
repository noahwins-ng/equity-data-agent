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

### Sensor-triggered runs fail with `gRPC UNAVAILABLE` during deploy

**Symptoms**:

- Dagster UI shows one or more runs marked `Failure` with the run-event sequence:
  ```
  EngineEvent: Unexpected error in IPC client
   → DagsterUserCodeUnreachableError: Could not reach user code server. gRPC Error code: UNAVAILABLE
  EngineEvent: Caught an unrecoverable error while dequeuing the run. Marking the run as failed…
  ```
- Asset checks attached to the intended outputs show `EXECUTION_FAILED` even though the asset never ran.
- Timing correlates with a recent `docker compose up -d` / CD deploy (code-server container was mid-restart).

**Diagnosis**:

```bash
# Confirm a deploy was in flight when the run was dequeued
gh run list --workflow=deploy.yml --limit 5                           # correlate timestamps

# Inspect the failed run's event log in the Dagster UI or via CLI
ssh hetzner 'docker exec equity-data-agent-dagster-daemon-1 \
    dagster run list --limit 10'                                       # grab run IDs
# Then view events in the UI — look for the IPC client / gRPC UNAVAILABLE sequence above.

# Check current retry config is active (QNT-110)
ssh hetzner 'docker exec equity-data-agent-dagster-daemon-1 \
    cat /opt/dagster/dagster_home/dagster.yaml 2>/dev/null || \
    grep -A 3 "run_retries" /opt/equity-data-agent/dagster.yaml'
# Expect: run_retries.enabled: true, retry_on_asset_or_op_failure: false
```

**Response**:

1. **If QNT-110 run_retries is active**: the daemon automatically re-launches the failed run up to 3 times (30s → 60s → 120s backoff). Wait ~4 min, then check the run's retry chain in the UI — the original run has a sibling tagged `dagster/parent_run_id`. No manual action needed unless all retries exhaust.
2. **If retries exhaust or retry config is missing**: manually re-materialize the affected asset partitions:
   ```bash
   ssh hetzner 'docker exec equity-data-agent-dagster-daemon-1 \
       /app/.venv/bin/dagster asset materialize \
       --select <asset_name> --partition <TICKER> \
       -m dagster_pipelines.definitions'
   ```
3. **If EXECUTION_FAILED asset checks are showing**: those are ghost failures — the checks never ran because the run never started. After re-materialization succeeds, the checks will re-evaluate.

**Prevention**:

- [QNT-110](https://linear.app/noahwins/issue/QNT-110) — two-layer retry protection. Run-level `run_retries` in `dagster.yaml` (enabled globally, opt-in per-job via `dagster/max_retries` tag) re-launches runs on launch-time failures. Op-level `DEPLOY_WINDOW_RETRY` (applied to sensor jobs) covers in-run transient failures. `retry_on_asset_or_op_failure: false` ensures only launch failures retry — real op errors still fail loud.
- [QNT-109](https://linear.app/noahwins/issue/QNT-109) — deploy sentinel suppresses Discord notifier noise during deploys, so retried-and-succeeded runs don't generate false alarms.

**Last occurred**: 2026-04-19 (before QNT-110)

---

### `dagster.yaml` config change didn't activate (named-volume shadowing)

**Symptoms**:

- You edited `dagster.yaml` in the repo, merged, CD was green, all hard gates passed — but the runtime behavior you configured hasn't changed. Example: bumped `run_retries.max_retries` but `DagsterInstance.run_retries_max_retries` still reports the old value; tweaked `code_servers.local_startup_timeout` but cold starts still time out at the old limit.
- `ssh hetzner 'cat /opt/equity-data-agent/dagster.yaml'` shows the new content.
- `ssh hetzner 'docker exec equity-data-agent-dagster-daemon-1 cat /dagster_home/dagster.yaml'` shows **stale** content.

**Diagnosis**:

```bash
# Compare repo file vs container file — they should be identical after QNT-112
ssh hetzner 'diff /opt/equity-data-agent/dagster.yaml \
    <(docker exec equity-data-agent-dagster-daemon-1 cat /dagster_home/dagster.yaml)'
# No output = aligned. Any output = named volume is shadowing the repo file.

# Confirm the bind mount is in place (QNT-112)
ssh hetzner 'docker inspect equity-data-agent-dagster-daemon-1 \
    --format "{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}"' | grep dagster.yaml
# Expect: /opt/equity-data-agent/dagster.yaml -> /dagster_home/dagster.yaml

# Query the running instance — the ultimate source of truth
ssh hetzner 'docker exec equity-data-agent-dagster-daemon-1 python -c "
from dagster import DagsterInstance
i = DagsterInstance.get()
print(\"run_retries:\", i.run_retries_enabled, i.run_retries_max_retries, i.run_retries_retry_on_asset_or_op_failure)
"'
```

**Response**:

1. **If QNT-112 bind mount is present but file still stale**: restart the daemon to pick up the new file — `ssh hetzner 'docker compose -f /opt/equity-data-agent/docker-compose.yml restart dagster-daemon dagster'`. The bind mount itself only delivers the file on container start, not live.
2. **If bind mount is missing** (e.g. pre-QNT-112 deploy): workaround via `docker cp` + restart:
   ```bash
   ssh hetzner 'docker cp /opt/equity-data-agent/dagster.yaml equity-data-agent-dagster-daemon-1:/dagster_home/dagster.yaml && \
                docker cp /opt/equity-data-agent/dagster.yaml equity-data-agent-dagster-1:/dagster_home/dagster.yaml && \
                docker compose -f /opt/equity-data-agent/docker-compose.yml restart dagster-daemon dagster'
   ```
   Then file a ticket to re-apply the QNT-112 bind mount for the affected path.
3. **Verify recovery**: re-run the `DagsterInstance.get()` attr check above. Values should match the repo's `dagster.yaml`.

**Prevention**:

- [QNT-112](https://linear.app/noahwins/issue/QNT-112) — bind-mounts `./dagster.yaml:/dagster_home/dagster.yaml:ro` so repo edits reach the container on every deploy. The named `dagster_home` volume continues to hold Dagster-managed state (history, storage, schedules) but no longer shadows config.
- Memory: `feedback_named_volume_shadows_repo_config.md` — flag this as first hypothesis whenever "config in repo ≠ runtime behavior" appears. Named volumes shadow any path under their mountpoint.

**Last occurred**: 2026-04-20 (QNT-110 ship session — `run_retries` config silently dropped; QNT-112 is the structural fix)

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

### Sick but still up containers (healthcheck unhealthy, no crash)

**Symptoms**:

- `docker compose ps` or `make check-prod` shows the container `Up`, but users see persistent HTTP 500s, stale data, or extremely slow responses.
- `/health` (API) may return 503 or time out, while the container process stays alive.
- No OOM kill, no container exit — the process is running but wedged (deadlock, blocked on I/O, exhausted worker pool, zombie httpx pool, stuck event loop).

**What handles this automatically**:

[QNT-104](https://linear.app/noahwins/issue/QNT-104) deploys the `willfarrell/autoheal` sidecar on the prod compose stack. It polls Docker's healthcheck status every 10 s and issues `POST /containers/{id}/kill` on any container labeled `autoheal=true` whose state is `unhealthy`. Combined with `restart: unless-stopped`, this auto-recovers within roughly **healthcheck-interval × healthcheck-retries + autoheal-interval** (≈ 30 s × 3 + 10 s = ~100 s) of the first unhealthy probe.

Services covered by the sidecar (label `autoheal=true` in `docker-compose.yml`): `api`, `dagster-code-server`, `dagster-daemon`, `dagster`, `litellm`. Run-worker containers (DockerRunLauncher, ADR-010) are **not** in this set — they get their own per-run health check via Dagster's `supports_check_run_worker_health = True`, with ~2 min STARTED-orphan recovery.

Services intentionally **not** labeled (and why):
- `clickhouse` — natural unhealthy windows under heavy merge load; killing it during a merge would orphan the merge and force re-execution. Healthcheck remains as a detector.
- `observability` stack (`prometheus`, `grafana`, `cadvisor`, `dozzle`) — secondary infra; if they wedge, page a human, don't loop-restart against an unknown failure.
- `cloudflared` — autoheal restart is safe with the named tunnel (QNT-177): the connector reauthenticates with the same token and the public hostname stays bound. Currently still excluded out of conservatism — revisit if false-positive autoheal restarts cause noise. Healthcheck remains as a detector.

**Diagnosis**:

```bash
# Healthcheck state on the suspected service — expect "healthy"; wedged → "unhealthy"
ssh hetzner 'docker inspect equity-data-agent-api-1 --format "{{.State.Health.Status}}"'

# Recent healthcheck probes (what the check actually returned)
ssh hetzner 'docker inspect equity-data-agent-api-1 --format "{{json .State.Health.Log}}" | jq .'

# Verify autoheal itself is up and watching
ssh hetzner 'docker compose -f /opt/equity-data-agent/docker-compose.yml ps autoheal'
ssh hetzner 'docker logs equity-data-agent-autoheal-1 --tail 20'
# Expect log lines like: "Container /equity-data-agent-... is unhealthy" → "Restarting container..."

# Resource pressure — near-limit memory or CPU suggests leaking / saturated service
ssh hetzner 'docker stats --no-stream equity-data-agent-api-1'
```

**Response**:

1. **First, wait ~100 s.** Autoheal should have killed the wedged container by now and `restart: unless-stopped` should have brought it back. Re-run `make check-prod`.
2. If still unhealthy after 2 minutes, autoheal itself may be down. Bring it back:
   ```bash
   ssh hetzner 'cd /opt/equity-data-agent && docker compose --profile prod up -d autoheal'
   ```
3. If autoheal is up but the target container is in a kill-restart loop (autoheal kills it, it boots, fails healthcheck, autoheal kills it again), the wedge is now a startup failure — fall through to **Container crash loop** above. Inspect logs for the window before the first wedge:
   ```bash
   ssh hetzner 'docker compose -f /opt/equity-data-agent/docker-compose.yml logs <service> --since 10m --tail 200'
   ```
4. If the service must NOT be auto-killed (e.g. mid-debugging a wedge), pause the autoheal sidecar instead — Docker labels are immutable on a running container, so the supported way to disable autoheal for a debug window is to stop autoheal itself, not to mutate the target's labels:
   ```bash
   # Pause autoheal (SIGSTOP — preserves state for resume; no restart needed)
   ssh hetzner 'docker pause equity-data-agent-autoheal-1'
   # …debug freely; the wedged container will not be killed…
   ssh hetzner 'docker unpause equity-data-agent-autoheal-1'
   ```
   If you need to *permanently* exempt a service, edit `docker-compose.yml` to remove its `autoheal: "true"` label, then `docker compose --profile prod up -d <service>` to recreate without the label.

**Common false-positive scenarios**:

- **Cold start after deploy** — Docker reports `starting` until `healthcheck.start_period` elapses; autoheal does not act on `starting`, only on `unhealthy`. So a slow-booting service is safe.
- **Brief healthcheck flap during heavy GC / batch import** — the healthcheck has `retries: 3`, so it must miss 3 consecutive probes (~90 s) before the container is marked unhealthy. Autoheal then kills within 10 s. Total: ~100 s. Brief saturation does not trigger.
- **CD restart of a service** — `docker compose --profile prod restart <svc>` deliberately stops the container; the QNT-109 deploy-window suppresses the kill event from the Discord alerter, but does **not** prevent autoheal from restarting the service afterwards (it just doesn't shout about it).

**Verifying autoheal end-to-end** (post-deploy check):

```bash
# 1. Pick a labeled service (api is cheapest to restart)
ssh hetzner 'docker exec equity-data-agent-api-1 sh -c "kill -STOP 1"'  # halt PID 1
# 2. Wait ~100 s, then verify the container was killed and replaced:
ssh hetzner 'docker ps --filter name=equity-data-agent-api-1 --format "{{.Status}}"'
# Expected: "Up <Ns>" (low number = recently restarted by autoheal)
# 3. Confirm the kill fired the docker-events-notify Discord alert (QNT-101).
```

**Prevention**:

- [QNT-100](https://linear.app/noahwins/issue/QNT-100) — compose-level HEALTHCHECK on every service surfaces the wedged state in `docker inspect` + `docker compose ps` + log UIs.
- [QNT-104](https://linear.app/noahwins/issue/QNT-104) — autoheal sidecar closes the loop: detection → kill → restart, no human in the path. Auto-recovery within ~100 s of going unhealthy.

**Last occurred**: not yet occurred — preventative

---

### Dagster backfill OOM-kill (run fan-out exceeds daemon cgroup)

**Symptoms**:

- A manually launched backfill (e.g. `fundamentals_weekly_job` 10-partition) fails after several minutes with some partitions "Failed to start".
- `[OOM KILL] equity-data-agent-dagster-daemon-1 exit=n/a` messages from `docker-events-notify` Discord webhook.
- Dagster run history shows per-partition runs labelled `Failure — Failed to start`, then 8/10 succeed on retry after the daemon restarts.
- `docker ps` shows `dagster-daemon` recently restarted (minutes-ago uptime).
- Daemon container itself stays green on `docker stats` (~250 MB post-restart) — it's *child* subprocesses being killed, not the daemon.

**Diagnosis**:

```bash
# OOM events in the daemon cgroup (note the task= value — "python" means a run-worker subprocess, not the daemon itself)
ssh hetzner 'journalctl -k --since "1 hour ago" | grep -E "Memory cgroup out of memory" | tail -10'

# Per-victim total-vm — expect ~2 GB VM, ~360 MB RSS per killed python child
# (per-worker peak revised up from 150 MB after Apr 21 __ASSET_JOB OOM — QNT-115)
ssh hetzner 'journalctl -k --since "1 hour ago" | grep "Killed process .* (python)" | tail -10'

# Confirm the daemon's cgroup is the one that OOM'd (match the container scope ID)
ssh hetzner 'docker inspect equity-data-agent-dagster-daemon-1 --format "{{.Id}}"'
```

**Response**:

1. No action needed on already-killed workers — [QNT-110](https://linear.app/noahwins/issue/QNT-110) run-retry relaunches them once the cgroup has headroom.
2. Confirm the run_coordinator cap is in the live config (not shadowed by a stale named-volume — see the "dagster.yaml config change didn't activate" entry above):

   ```bash
   ssh hetzner 'docker exec equity-data-agent-dagster-daemon-1 cat /dagster_home/dagster.yaml | grep -A4 run_coordinator'
   ```
3. If `max_concurrent_runs` is missing or too high, fix `dagster.yaml` in-repo, ship through CD. Never SCP-patch prod (see [QNT-107](https://linear.app/noahwins/issue/QNT-107)).

**Prevention**:

- [QNT-116](https://linear.app/noahwins/issue/QNT-116) — **structural fix.** Production-topology migration: user code moved out of the daemon cgroup into a dedicated `dagster-code-server` container, and runs launch in ephemeral Docker containers via `DockerRunLauncher` instead of subprocesses inside the daemon cgroup. Per-run OOM is now isolated to a single ephemeral container rather than killing siblings or blowing the daemon's cgroup. Daemon `mem_limit: 3g → 512m` post-migration. The QNT-111/113/115 "bump mem_limit again" cycle is retired.
- [QNT-113](https://linear.app/noahwins/issue/QNT-113) — `QueuedRunCoordinator(max_concurrent_runs=3)` in `dagster.yaml` serialises backfill fan-out; still active post-QNT-116 because it's enforced at the run-coordinator layer (fan-out control), not via the launcher.
- **Memory math** (historical, pre-QNT-116 — retained for archaeology; post-QNT-116 the daemon cgroup is no longer the binding constraint):
  - Daemon baseline: ~260 MB
  - Sensor-tick subprocess headroom: ~400 MB
  - N workers × ~360 MB RSS each (peak during `__ASSET_JOB` materialization; revised from 150 MB — QNT-115)
  - With pre-QNT-116 `mem_limit: 3g` and `max_concurrent_runs: 3`, peak ≈ 1.74 GB (leaves ~1.3 GB slack for materialization spikes).
  - Post-QNT-116, each run gets its own container with its own cgroup — run-worker memory no longer accumulates under a single cgroup, so the formula becomes per-run rather than N-aggregate.
- [QNT-110](https://linear.app/noahwins/issue/QNT-110) run-retry is complementary — handles transient launch failures (gRPC UNAVAILABLE, etc.).
- [QNT-114](https://linear.app/noahwins/issue/QNT-114) `run_monitoring` auto-fails STARTED/CANCELING runs whose worker was OOM-killed before emitting `RUN_FAILURE` — see "CANCELING ghost after run-worker OOM" below. Post-QNT-116 the STARTED branch also fires (DockerRunLauncher supports per-worker health check), recovery ~2 min instead of ~30.

**Last occurred**: 2026-04-21 12:22 / 12:48 UTC — manual 3-partition `__ASSET_JOB` backfill (MSFT/GOOGL/AMZN); two kernel OOM-kills of `python` run-worker subprocesses in the daemon cgroup at the 2 GB limit. Observed daemon peak 1.74 GiB (87% of limit). Root cause: QNT-113 sizing math assumed 150 MB/worker but real peak during `__ASSET_JOB` materialization is ~360 MB. Follow-up: QNT-115 bumped `mem_limit` 2g → 3g.

Prior: 2026-04-20 13:22–13:28 UTC — manually launched 10-partition backfill on `fundamentals_weekly_job` via Dagster UI; 3 kernel OOM kills in the daemon cgroup, backfill `tevuzzoj` failed after 10:31, partition AMZN (`5138c8ee`) stuck at "Failed to start".

---

### CANCELING ghost after run-worker OOM (orphaned STARTED run wedges queue)

**Symptoms**:

- Dagster run history shows a run stuck in **STARTED** or **CANCELING** long after its expected completion (minutes, not seconds).
- Clicking "Terminate" in the UI flips the row to CANCELING but it never progresses to FAILURE — CANCELING requires the run-worker to ack, and the worker is already dead.
- One of `max_concurrent_runs` slots is held by the ghost row, so new runs (sensor ticks, backfill partitions) queue indefinitely.
- Backfill daemon keeps auto-relaunching the same partition, producing additional ghost rows — every retry just adds another stuck STARTED row.
- `docker logs equity-data-agent-dagster-daemon-1` does NOT show a `RUN_FAILURE` event for the ghost; the worker died before it could emit one. Typically follows a `[OOM KILL]` Discord alert on the daemon container.

**Diagnosis**:

```bash
# Find ghost runs: STARTED/STARTING/CANCELING for >> expected runtime
ssh hetzner 'docker exec equity-data-agent-dagster-daemon-1 dagster run list --status STARTED --limit 20'
ssh hetzner 'docker exec equity-data-agent-dagster-daemon-1 dagster run list --status CANCELING --limit 20'

# Cross-check against kernel OOM kills — ghost runs almost always correlate 1:1
# with a "Killed process ... (python)" kernel log within the last hour.
ssh hetzner 'journalctl -k --since "1 hour ago" | grep "Killed process .* (python)" | tail -10'

# Confirm run_monitoring is actually enabled in the live config
ssh hetzner 'docker exec equity-data-agent-dagster-daemon-1 cat /dagster_home/dagster.yaml | grep -A6 run_monitoring'
```

**Response** — depends on which orphan class you have. Check the row's current status first:

```bash
ssh hetzner 'docker exec equity-data-agent-dagster-daemon-1 dagster run list --status STARTED,CANCELING --limit 20'
```

1. **CANCELING ghost** (operator hit Terminate, run flipped to CANCELING, worker was already dead so no ack comes back — **this is what the Apr 21 AMZN incident was**): `monitor_canceling_run` auto-fails it 180s (`cancel_timeout_seconds`) after the `RUN_CANCELING` event, regardless of launcher type — no worker health check needed. Wait up to 3 min, confirm with `dagster run list --status FAILURE | head`. Queue slot is released on the FAILURE.
2. **STARTED ghost with no operator Terminate** (worker died mid-execution, the run is just sitting STARTED with no RUN_FAILURE event): with `DefaultRunLauncher` on our current setup, `monitor_started_run`'s worker-health branch is skipped (launcher returns `supports_check_run_worker_health = False`). The only recovery is the timeout fallback `max_runtime_seconds: 1800` — any STARTED run older than 30 min gets failed on the next poll. Wait up to 30 min. If the queue is actively blocked and can't wait, proceed to step 4.
3. **STARTING ghost** (launch never completed, run stuck in STARTING): `monitor_starting_run` auto-fails it 180s (`start_timeout_seconds`) after launch. Wait up to 3 min.
4. If `run_monitoring` is disabled/missing, or you can't wait for the 30-min STARTED timeout and the queue is wedging time-sensitive work: restart the daemon to release the in-memory slot counter. This does **not** reconcile the ghost rows — they stay STARTED in the run-storage DB — but the restart lets new runs dequeue:
   ```bash
   ssh hetzner 'docker compose --profile prod restart dagster-daemon'
   ```
   Then fix `dagster.yaml` in-repo, ship through CD. Never SCP-patch prod (see [QNT-107](https://linear.app/noahwins/issue/QNT-107)). Once monitoring is live, the stale STARTED rows auto-reconcile per step 2; CANCELING per step 1.
5. Only as last resort, if the queue is blocked on a STARTED ghost and 30 min is too long: mark it failed manually via Dagster UI "Mark as failure" action. This releases the slot but does not emit the normal run-failure events (no Sentry hook, no [QNT-110](https://linear.app/noahwins/issue/QNT-110) retry). Prefer step 2 unless the queue pressure is blocking something time-sensitive.

**Prevention**:

[QNT-114](https://linear.app/noahwins/issue/QNT-114)'s `run_monitoring` block in `dagster.yaml` covers three orphan classes with different recovery speeds:

| Orphan class | Monitor function | Timeout knob | Recovery time | Launcher-dependent? |
|---|---|---|---|---|
| STARTING never completes launch | `monitor_starting_run` | `start_timeout_seconds: 180` | ~3 min | No |
| CANCELING stuck after Terminate | `monitor_canceling_run` | `cancel_timeout_seconds: 180` | ~3 min | No |
| STARTED worker died, no Terminate | `monitor_started_run` → per-worker health check | `poll_interval_seconds: 120` | ~2 min | No — post-QNT-116 we're on `DockerRunLauncher`, which returns `supports_check_run_worker_health = True`. `max_runtime_seconds: 1800` stays as a floor for legitimately long-running ops, not as the primary recovery path. |

Notes:

- `max_resume_run_attempts: 0` means Dagster **fails** orphans instead of resuming them — resumption would just re-OOM the same worker in the same cgroup.
- The Apr 21 AMZN incident was the **CANCELING class** (operator hit Terminate). PR #94 enables `run_monitoring` → that incident recovers in ~3 min via `monitor_canceling_run` even without the hotfix's `max_runtime_seconds`. The hotfix PR #95 closes the separate STARTED-no-Terminate class which PR #94 didn't cover because `DefaultRunLauncher` couldn't health-check workers.
- [QNT-116](https://linear.app/noahwins/issue/QNT-116) migrated to `DockerRunLauncher`, collapsing STARTED-orphan recovery from ~30 min to ~2 min by activating the per-worker health path.
- Complementary to [QNT-110](https://linear.app/noahwins/issue/QNT-110) (launch-time retry) and [QNT-113](https://linear.app/noahwins/issue/QNT-113) (fan-out cap): QNT-113 limits new fan-out, QNT-114 cleans up after OOMs when they still happen.
- [QNT-114](https://linear.app/noahwins/issue/QNT-114)'s `tag_concurrency_limits` reserves ≥1 of 3 slots for non-backfill work (`dagster/backfill` capped at 2), so a 3-partition backfill can't starve sensors even before any runs go ghost.

**Last occurred**: 2026-04-21 — post-incident backfill of `__ASSET_JOB` (MSFT/GOOGL/AMZN); AMZN worker kernel-OOM'd before emitting RUN_FAILURE; operator Terminate → CANCELING → stuck; backfill daemon relaunched AMZN, producing more ghosts; queue wedged ~30 min until daemon restart.

---

### Credential compromise suspected

**Symptoms**:

- Unexpected API usage / billing spike on Anthropic, Ollama, Qdrant, or Langfuse dashboards.
- Auth failures on legitimate calls (a rotation you didn't initiate happened upstream).
- Unfamiliar entries in `/var/log/auth.log` on Hetzner (`last`, `lastb`) — successful SSH logins from unknown IPs, repeated failed-login bursts.
- Out-of-band notification (provider abuse team, GitHub secret-scanning alert, Gitleaks CI failure).

**Diagnosis**:

```bash
# Recent SSH logins on prod — flag anything outside expected IPs
ssh hetzner 'last -n 20 -i'
ssh hetzner 'grep sshd /var/log/auth.log | grep -E "Accepted|Failed" | tail -40'

# Current .env on prod — timestamp should match the most recent CD run
ssh hetzner 'stat /opt/equity-data-agent/.env'

# Is the plaintext .env file-mode tight? (0600 root:root)
ssh hetzner 'ls -la /opt/equity-data-agent/.env'

# Has anyone pushed unauthorised commits? Check for edits to .env.sops or .sops.yaml
git log --all --oneline -- .env.sops .sops.yaml | head -20

# GitHub audit: recent secret access / PAT use / Actions runs
gh api /repos/noahwins-ng/equity-data-agent/actions/runs --jq '.workflow_runs[0:5][] | {name, head_sha, actor: .actor.login, created_at}'
```

**Response**:

1. **Rotate the upstream values first** — whatever credential is suspected leaked, invalidate it at the provider before anything else:
   - Anthropic: API Keys dashboard → revoke → generate new.
   - Ollama Cloud: account settings → revoke → generate new.
   - Qdrant Cloud: cluster API keys → revoke → generate new.
   - Langfuse: project settings → rotate public + secret keys.
   - Any provider not on this list: check `.env.example` for the full set.
2. **Update `.env.sops` with the new values** — `make sops-edit`, edit the affected keys, save, commit, push. CD will pick up the new values on the next deploy.
3. **If the SOPS age key itself may be compromised** (e.g. laptop stolen, key pushed to a public repo, credentials leaked via a key-logger): follow `docs/guides/hetzner-bootstrap.md` §11.3 to rotate the age key. This re-encrypts `.env.sops` under a new recipient and updates the GH secret.
4. **If the Hetzner SSH key may be compromised**:
   ```bash
   # Generate a new deploy key locally
   ssh-keygen -t ed25519 -f ~/.ssh/hetzner_deploy_new -N ''
   # Install the new public key on Hetzner (via the old key, or via Hetzner console if the old key is revoked)
   ssh-copy-id -i ~/.ssh/hetzner_deploy_new.pub hetzner
   # Remove the old public key from ~/.ssh/authorized_keys
   ssh hetzner "grep -v 'OLD-KEY-COMMENT' ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.new && mv ~/.ssh/authorized_keys.new ~/.ssh/authorized_keys"
   # Update the HETZNER_SSH_KEY GitHub secret to the new private key
   ```
5. **Verify the blast radius**: after rotation, re-check upstream dashboards for usage attributed to the old credentials. Billing anomalies that persist after rotation point to a deeper compromise (VPS container escape, supply-chain); escalate to `make check-prod` + full image rebuild if so.
6. **Document the incident**: append a post-mortem note to this runbook entry's "Last occurred" line with date, attack vector (if known), and what was rotated. Feed findings back into `feedback_*` memory so future sessions benefit.

**Prevention**:

- [QNT-102](https://linear.app/noahwins/issue/QNT-102) — SOPS encryption at rest means the `.env.sops` in git is not a useful target on its own (ciphertext without the age key). The age key is split across the GH secret (for CD) and a password manager (for humans); compromising one doesn't compromise the other.
- CD-managed `.env` on VPS (mode 0600, root:root, overwritten every deploy) limits the exposure window — any values manually pasted onto the VPS (or left behind from a prior rotation) are wiped by the next push to `main`.
- No long-lived dev credentials: keep `ANTHROPIC_API_KEY` / provider keys scoped per-project when possible; rotate on team-membership changes.
- `docs/guides/hetzner-bootstrap.md` §11.3 rotation workflow is non-trivial on purpose — rotating should be routine (quarterly or after any suspected exposure), not rare-and-scary.

**Last occurred**: not yet occurred — preventative

---

### Slow memory leak suspected (climbing RSS, no immediate OOM)

**Symptoms**:

- Discord `[OOM KILL]` alerts firing sporadically on the same service over days, with growing intervals between healthy periods.
- Container restarts in the per-container restart panel of the *Containers Overview* Grafana dashboard, but no obvious crash cause in `docker logs` (the killer is the kernel cgroup OOM, not the app).
- `make obs-status` host headroom drops below 3 GiB during normal load (CX41 has 16 GiB total).

**Diagnosis**:

```bash
# 1. Identify the leaking service: open Grafana → "Containers Overview" → "Memory % of mem_limit per container",
#    extend range to "Last 7 days". A leaking service is a sloped-up line; a healthy one is flat-with-noise.
make tunnel  # then visit http://localhost:3030

# 2. Confirm via PromQL directly (no UI tunnel needed)
ssh hetzner 'curl -sf "http://localhost:9090/api/v1/query?query=container_memory_working_set_bytes%7Bname%3D~%22equity-data-agent-.%2B%22%7D" | python3 -m json.tool'

# 3. Check current Grafana alert state
ssh hetzner 'curl -sf -u admin:<password> http://localhost:3030/api/v1/provisioning/alert-rules | python3 -m json.tool | head -40'
```

**Response**:

1. **Identify leak class**: unbounded in-process cache, leaked client connection, unclosed file/socket handle. `docker exec equity-data-agent-<svc>-1 sh -c 'ls /proc/1/fd/ | wc -l'` → if growing day-over-day, it's an FD leak.
2. **Patch in code, ship via CD** (never `scp`-hotfix — `feedback_prod_hotfix_scp.md`).
3. **Short-term mitigation if patch isn't immediate**: schedule a daily `docker compose --profile prod restart <svc>` via cron until the leak is fixed. Document the workaround in this runbook.
4. **Verify post-fix**: leave Grafana's per-container memory panel open for 48 h after the deploy — flat line confirms the leak is closed.

**Prevention**:

- [QNT-103](https://linear.app/noahwins/issue/QNT-103) — Prometheus + cAdvisor scrape every 30 s with 15 d retention; the *Containers Overview* dashboard is the canonical view of long-window memory trends. The `ContainerMemoryHigh` alert fires Discord at >80% of mem_limit for 5 min, well before the kernel OOM-kills the container at 100%.
- `mem_limit` per service in `docker-compose.yml` is the safety net — leaks crash the offending container, not its neighbours.

**Last occurred**: not yet occurred — preventative (capability landed with QNT-103).

---

### Disk usage climbing on prod (`HostDiskHigh` alert, /dev/sda1 > 80%)

**Symptoms**:

- Discord `HostDiskHigh` alert fires (rule lives in `observability/grafana/provisioning/alerting/rules.yaml`, threshold 80% on `/`).
- *Host Overview* Grafana dashboard "Disk usage %" panel red.
- `docker system df` on prod shows hundreds-to-thousands of `Containers` rows under TOTAL with very few ACTIVE (1080 / 16 was the QNT-167 baseline), or many GB of `Build Cache` unreferenced by any tagged image.

**Diagnosis**:

```bash
# 1. Confirm host-level pressure
ssh hetzner 'df -h /'

# 2. Break down where the GB live (containers, images, volumes, build cache)
ssh hetzner 'docker system df'

# 3. If Containers is the culprit, who left them behind?
#    Run-worker containers from DockerRunLauncher should auto-remove (QNT-167);
#    if you see Exited equity-data-agent-dagster:latest containers, auto_remove
#    is missing from dagster.yaml or didn't reach the daemon (named-volume
#    shadowing — see "dagster.yaml config change didn't activate").
ssh hetzner 'docker ps -a --filter ancestor=equity-data-agent-dagster:latest --format "{{.Names}}\t{{.Status}}" | head -20'
```

**Response**:

```bash
# Stopped containers + dangling images + unreferenced build cache. Running
# services and named volumes are NOT touched. Safe to run during business hours.
ssh hetzner '
  docker container prune -f
  docker builder prune -f --filter unused-for=24h
  docker image prune -f
'

# Verify disk dropped
make obs-status
```

**Prevention**:

- [QNT-167](https://linear.app/noahwins/issue/QNT-167) — `auto_remove: true` on `DockerRunLauncher.container_kwargs` in `dagster.yaml`. Every run-worker container is removed by Docker the moment it exits, eliminating the accumulation at the source. Logs + event log are persisted in the SQLite store on the `dagster_home` volume, so removing the container loses no debugging signal.
- [QNT-167](https://linear.app/noahwins/issue/QNT-167) — `make prune-build-cache-install` lays down a weekly cron (Sun 04:00 UTC) that runs `docker builder prune --filter unused-for=168h`. Build cache grows ~1-2 GB per CD `--build`; weekly hygiene keeps it bounded.
- [QNT-103](https://linear.app/noahwins/issue/QNT-103) — `HostDiskHigh` Grafana alert (>80% for 10 min) gives early warning long before the kernel ENOSPCs writes.

**Last occurred**: 2026-05-03 — surfaced by `HostDiskHigh` after QNT-103 landed; 1080 stopped run-worker containers + 53 GB build cache, /dev/sda1 at 85%.

---

### ClickHouse system-log creep (text_log/trace_log/metric_log/async_metric_log unbounded growth)

**Symptoms**:

- `clickhouse-1` CPU climbing monotonically over hours/days (e.g. 0.07 → 0.18 cores over 5.5 h on 2026-05-04, peaking at 72% during merge bursts) with no corresponding user-query load.
- `equity_raw` / `equity_derived` tables look healthy (row counts and sizes consistent with retention); cost-of-merge is dominated by `system.*_log` tables instead.
- Disk usage on `clickhouse_data` volume creeps up day-over-day even on idle prod.

**Diagnosis**:

```bash
# 1. Confirm the climb is system-table merges, not user data. ProfileEvent_MergeTotalMilliseconds
#    should show monotonic increase keyed to system tables.
ssh hetzner 'docker exec equity-data-agent-clickhouse-1 clickhouse-client --query "
  SELECT table, sum(rows) AS rows, formatReadableSize(sum(bytes_on_disk)) AS bytes
  FROM system.parts WHERE database = '\''system'\'' AND active
  GROUP BY table ORDER BY sum(bytes_on_disk) DESC LIMIT 10
"'

# 2. Confirm whether TTL is set on the noisy tables (post-fix this should
#    show "TTL event_date + toIntervalDay(30)" on all four).
ssh hetzner 'docker exec equity-data-agent-clickhouse-1 clickhouse-client --query "
  SELECT name, engine_full FROM system.tables
  WHERE database = '\''system'\'' AND name IN ('\''text_log'\'','\''trace_log'\'','\''metric_log'\'','\''asynchronous_metric_log'\'')
"'

# 3. Are there leftover renamed tables from a prior schema change?
ssh hetzner 'docker exec equity-data-agent-clickhouse-1 clickhouse-client --query "
  SELECT name, total_rows, formatReadableSize(total_bytes) FROM system.tables
  WHERE database = '\''system'\'' AND name LIKE '\''%_log_%'\''
"'
```

**Response** (assumes the TTL config from QNT-169 is already in `clickhouse/config.d/system-log-ttl.xml` and merged):

1. **Deploy the config** (CD via merge to main, or manual `docker compose --profile prod up -d clickhouse` after pulling). ClickHouse must restart to pick up `/etc/clickhouse-server/config.d/system-log-ttl.xml`.
2. **Restart renames the existing tables.** Because partition_by changes from monthly to daily, ClickHouse will move `system.text_log` (etc.) to `system.text_log_0` and create fresh tables with the new schema + TTL. The renamed tables retain all historical rows and are NOT covered by the new TTL.
3. **Drop the renamed tables** to free disk and stop their merge cost. List them first via the diagnosis query #3 above (`SELECT name FROM system.tables WHERE database='system' AND name LIKE '%_log_%'`) — ClickHouse increments the rename suffix (`_0`, `_1`, …) on every schema change, so don't assume `_0`. Drop each one explicitly (one `--query` per statement; safest across CH versions):
   ```bash
   for t in text_log_0 trace_log_0 metric_log_0 asynchronous_metric_log_0; do
     ssh hetzner "docker exec equity-data-agent-clickhouse-1 clickhouse-client --query \"DROP TABLE IF EXISTS system.$t SYNC\""
   done
   ```
4. **Verify**: row counts on the new tables should stay within the 30-day window. After 24 h of production, run the diagnosis query #1 again — `text_log` should sit at 1-2 days of inserts (≈ 1M rows for this load), and after 30 days steady-state should plateau around ~30 days × 30-40k rows/hr (a few million rows). `clickhouse-1` CPU baseline should return to ~0.05 cores once the renamed-table backlog is dropped.

**Prevention**:

- [QNT-169](https://linear.app/noahwins/issue/QNT-169) — `clickhouse/config.d/system-log-ttl.xml` mounted into the `clickhouse` service. Sets `partition_by event_date` + `TTL event_date + INTERVAL 30 DAY DELETE` on all four tables (matches ClickHouse Cloud default), so ClickHouse drops whole daily parts as they age past the retention window.
- The same volume mount is wired in `docker-compose.yml`; if a future ClickHouse upgrade changes the default config schema, the override survives because `config.d/` files are loaded after and merged on top of the base `config.xml`.

**Last occurred**: 2026-05-04 — surfaced by Grafana CPU panel; triaged but not yet remediated until QNT-169 (this entry).

---

### Where are the logs / dashboards?

| What | URL (via SSH tunnel) | Auth | Purpose |
|---|---|---|---|
| Dozzle | `http://localhost:8082` | SSH tunnel only | Tail any compose container's logs without `ssh hetzner`. |
| Grafana | `http://localhost:3030` | `admin` / `admin` on first launch only — Grafana forces a password change at first login; the chosen password persists in `grafana_data` named volume across restarts. Store the new password in your password manager. To rotate later: `ssh hetzner 'docker exec equity-data-agent-grafana-1 grafana cli admin reset-admin-password <new>'`. | Host + per-container metrics, alert rules, alert history. |
| Prometheus | `http://localhost:9090` | SSH tunnel only | Raw PromQL access, scrape target health (`/targets`). |
| Dagster UI | `http://localhost:3100` | SSH tunnel only | Asset graph, run history, sensor ticks. |
| ClickHouse Play | `http://localhost:8123/play` | SSH tunnel only | Ad-hoc SQL against the warehouse. |

Open all observability tunnels with `make tunnel` (forwards 8123, 3100, 8082, 9090, 3030 in one process). Quick health snapshot without opening UIs: `make obs-status`.

**Synthetic alert verification** (run when wiring or rotating the Discord webhook): `make obs-alert-test` spins up `equity-data-agent-stress` with a 64 MiB mem_limit and stress-ng eating 90% of it for 360 s. Wait ~5 min — the `ContainerMemoryHigh` rule should transition to `Alerting` and post to Discord. Cleanup: `ssh hetzner 'docker stop equity-data-agent-stress'`.

**Did the obs stack actually wire up?** `make obs-smoke` is the authoritative answer (QNT-172). It asserts every Prometheus scrape target is healthy, every Grafana dashboard panel returns at least one series, every alert rule's underlying PromQL has data (so the rule isn't permanently NoData), and every bind-mounted config in `docker-compose.yml` has a matching `restart_if_(prefix_)changed` entry in `.github/workflows/deploy.yml`. CD runs the same script after `docker compose up -d` (using a one-shot container joined to `equity-data-agent_default`) and rolls back to the previous SHA on failure. This is the gate that catches the silent-NoData / empty-panel / never-fires class of regression that QNT-103 hit five times in 24 hours after ship — every signal said green while panels were empty (PR #197 cAdvisor `--docker_only`, PR #200 node_exporter mountpoint, PR #201 missing CD restart for `observability/`). Unit tests cannot catch these by design; if you change anything in `observability/` or in the deploy workflow, run `make obs-smoke` before merging.

**Resource footprint** (measured on CX41, idle stack — verify after first deploy with `make obs-status`):

| Service | mem_limit | Typical RSS | Notes |
|---|---|---|---|
| dozzle | 192 MiB | ~40 MiB idle | v8 reactively replays per-container tail logs on connect (QNT-164 bump from 64m after first-connect OOM). |
| node_exporter | 64 MiB | ~20 MiB | Host metrics, no state. |
| cadvisor | 256 MiB | ~120 MiB | Privileged; reads cgroup accounting. |
| prometheus | 384 MiB | ~180 MiB | TSDB grows with retention (15 d cap). |
| grafana | 256 MiB | ~110 MiB | UI + alert rule evaluation. |
| **Total observability** | **1152 MiB** | **~470 MiB** | Slightly over the original "<1 GiB" budget after QNT-164 dozzle bump; still well under host headroom. |

CX41 totals: 16 GiB RAM. Pre-QNT-103 mem_limit allocation was 13.06 GiB (clickhouse 8 GiB + dagster trio 3.5 GiB + api 1 GiB + litellm 0.5 GiB + cloudflared 64 MiB); post-QNT-103 + QNT-164 it's 14.19 GiB. Leaves ~1.81 GiB host headroom outside cgroups, plus mem_limit is a hard ceiling (typical RSS sits well below it) and reservations are softer than limits, so realised free memory under typical load should remain above the 3 GiB AC threshold. Verify post-deploy via `make obs-status`.

---

## Security notes

### Docker socket bind-mount on `dagster-daemon` (QNT-116)

**Context**: `DockerRunLauncher` requires the daemon to call the Docker API to start ephemeral run-worker containers. The daemon therefore has `/var/run/docker.sock` bind-mounted read-write.

**Trust boundary**: anyone who can execute code inside `dagster-daemon` can create/delete/inspect any container on the host, read all container filesystems, and escalate to host root via a privileged container. This is the standard Docker-socket caveat; it is not unique to Dagster.

**Mitigations in place**:
- The daemon image is built from the same repo Dockerfile as the rest of the stack — no third-party code runs in the daemon's main process. User code runs in `dagster-code-server` (separate service, no docker socket) and in ephemeral run workers (launched via DockerRunLauncher, no docker socket inside the run worker).
- The daemon's `command:` is pinned to `dagster-daemon run -w /app/workspace.yaml` — not a shell. Remote code execution would require exploiting a bug in `dagster-daemon` itself or in a dep it imports at startup.
- Host firewall (Hetzner `ufw`) restricts inbound ports to 22 + 80 + 443 + 8000 (API); the daemon is not directly reachable from the internet.
- `.env` is mode 0600, root:root on the host; even via the docker socket, extracting secrets requires privileged container access.

**Do not**: bind-mount `/var/run/docker.sock` into `dagster-code-server` or into ephemeral run workers. Only the daemon needs it.

### Docker socket bind-mount on `autoheal` (QNT-104)

**Context**: `willfarrell/autoheal` polls Docker's container-state API and issues `POST /containers/{id}/kill` on services labeled `autoheal=true` whose healthcheck transitions to `unhealthy`. It therefore has `/var/run/docker.sock` bind-mounted.

**Mount mode**: `:ro` is set on the bind. Note that this is a **socket-file-mode flag, not an API capability**: a process inside the container can still issue write commands (kill / start / stop) over the socket, because the Docker daemon authenticates the request at the socket-protocol layer — not via the read-only flag on the bind-mount inode. The `:ro` is defence-in-depth against the in-container process trying to `chmod` the socket inode itself; it is not a containment boundary.

**Trust boundary**: same class as the dagster-daemon mount above. Anyone with code execution inside the autoheal container has full Docker API control on the host.

**Mitigations in place**:
- Image is the upstream `willfarrell/autoheal:1.2.0` pinned tag (no `latest`). The container shell is the autoheal binary's `autoheal` script + `curl`; no Python interpreter, no compiler, no SSH client. RCE surface is the autoheal script itself + the libcurl version in the image.
- Container has no listening ports — it cannot be reached from inside the compose network or from the host.
- `restart: unless-stopped` will recreate the autoheal container from the pinned image on any kill, so a transient compromise does not persist beyond the next restart.
- Memory-limited to 32 MiB — cannot host a substantial implant in-process.

**Do not**: change the image to a `:latest` tag. The trust delta from a published image we audited (1.2.0) to a future floating tag is large enough to deserve an explicit upgrade ticket.
