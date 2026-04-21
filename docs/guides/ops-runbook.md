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

# Per-victim total-vm — expect ~2 GB VM, ~150 MB RSS per killed python child
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

- [QNT-113](https://linear.app/noahwins/issue/QNT-113) — `QueuedRunCoordinator(max_concurrent_runs=3)` in `dagster.yaml` serialises backfill fan-out so peak memory stays under the daemon's 2 GB cgroup.
- **Memory math** (must stay consistent with `mem_limit` on dagster-daemon in `docker-compose.yml`):
  - Daemon baseline: ~260 MB
  - Sensor-tick subprocess headroom: ~400 MB
  - N workers × ~150 MB RSS each
  - With `mem_limit: 2g` and `max_concurrent_runs: 3`, peak ≈ 1.1 GB (leaves ~900 MB slack for materialization spikes)
  - If the daemon's `mem_limit` is raised, `max_concurrent_runs` can rise proportionally: roughly `(mem_limit - 660MB) / 150MB`.
- [QNT-110](https://linear.app/noahwins/issue/QNT-110) run-retry is complementary — it handles transient launch failures but won't rescue a cgroup under sustained fan-out pressure (retries re-launch into the same starved cgroup).
- [QNT-114](https://linear.app/noahwins/issue/QNT-114) `run_monitoring` auto-fails STARTED/CANCELING runs whose worker was OOM-killed before emitting `RUN_FAILURE` — see "CANCELING ghost after run-worker OOM" below. Without this, a kernel-killed worker leaves a ghost slot that silently holds one of three `max_concurrent_runs` slots until the daemon is restarted.

**Last occurred**: 2026-04-20 13:22–13:28 UTC — manually launched 10-partition backfill on `fundamentals_weekly_job` via Dagster UI; 3 kernel OOM kills in the daemon cgroup, backfill `tevuzzoj` failed after 10:31, partition AMZN (`5138c8ee`) stuck at "Failed to start".

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
| STARTED worker died, no Terminate | `monitor_started_run` → `check_run_timeout` | `max_runtime_seconds: 1800` | ~30 min | **Yes** — only the timeout fallback fires on `DefaultRunLauncher`; container-aware launchers would fire worker-health check in ~2 min instead. |

Notes:

- `max_resume_run_attempts: 0` means Dagster **fails** orphans instead of resuming them — resumption would just re-OOM the same worker in the same cgroup.
- The Apr 21 AMZN incident was the **CANCELING class** (operator hit Terminate). PR #94 enables `run_monitoring` → that incident recovers in ~3 min via `monitor_canceling_run` even without the hotfix's `max_runtime_seconds`. The hotfix PR #95 closes the separate STARTED-no-Terminate class which PR #94 didn't cover because `DefaultRunLauncher` can't health-check workers.
- Switching to `DockerRunLauncher` (follow-up ticket) would collapse the STARTED recovery from 30 min → ~2 min by enabling the per-worker health path.
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
