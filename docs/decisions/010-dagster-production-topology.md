# ADR-010: Dagster Production Topology — Code-Server Split + DockerRunLauncher

**Date**: 2026-04-22
**Status**: Accepted

## Context

Our Dagster deployment (since Phase 2) has used the *beginner* single-process topology: one `dagster-daemon` container that runs the scheduler, sensor daemon, backfill daemon, run coordinator, *and* loads all user-code definitions in-process via `-m dagster_pipelines.definitions`. Every schedule tick, sensor evaluation, backfill iteration, and `DefaultRunLauncher`-launched run spawns a Python subprocess inside the daemon's cgroup, each importing the full repo (~360 MB peak RSS per QNT-115's revised observation).

This produced two independent failure patterns, both rooted in the same cause:

### Pattern 1 — The `mem_limit`-bump cycle

Over ~1 month the daemon's `mem_limit` was bumped reactively three times, each following an OOM incident:

- **QNT-100** (Apr-06): initial `mem_limit: 1g`.
- **QNT-111** (Apr-20): `1g → 2g` after a 10-partition `fundamentals_weekly_job` backfill OOM-cascaded.
- **QNT-115** (Apr-21 12:22 UTC): `2g → 3g` after a 3-partition `__ASSET_JOB` backfill OOM'd at 87 % cgroup saturation; QNT-113's sizing math had assumed 150 MB/worker but observed peak was ~360 MB.
- **QNT-115 incident** (Apr-21 21:13–21:26 UTC, hours after the `2g → 3g` bump): an *in-daemon-cgroup* OOM-kill of a code-loading subprocess rendered the code server unavailable for 180 s. 8 `ohlcv_downstream_job` runs transitioned to FAILURE without ever launching a step, 5 of them in lockstep at 21:26 as submission-path retries timed out simultaneously.

Each bump was a local fix — a ratchet rather than a structural change. The Apr-21 21:13 incident hit the ceiling: 3 g was already saturated under fan-out, so further bumping was not going to hold.

### Pattern 2 — The STARTED-orphan recovery hole

QNT-114's chaos test surfaced a separate issue: `DefaultRunLauncher` returns `supports_check_run_worker_health = False`, so `run_monitoring`'s `monitor_started_run` health branch is a no-op. STARTED-orphan recovery falls back to `max_runtime_seconds: 1800` (~30 min) instead of the container-aware ~2 min per-worker health check available to `DockerRunLauncher`/`K8sRunLauncher`.

Both patterns have the same root cause: **the daemon container is doing jobs that Dagster's production deployment docs explicitly carve out into separate services.**

## Decision

Adopt the **canonical Dagster OSS Docker Compose production topology** (https://docs.dagster.io/deployment/oss/deployment-options/docker):

1. **Split user code into its own service** (`dagster-code-server`).
   - Runs `dagster code-server start -h 0.0.0.0 -p 4000 -m dagster_pipelines.definitions`.
   - Serves the `equity_pipelines` code location over gRPC.
   - `mem_limit: 2g` — isolated from daemon, generous without bleeding into scheduler/sensor work.
   - Healthcheck: `dagster api grpc-health-check -p 4000`.
   - Exports `DAGSTER_CURRENT_IMAGE=equity-data-agent-dagster:latest` so DockerRunLauncher (item 3 below) can infer the image for ephemeral run workers.

2. **Webserver and daemon reach user code via gRPC** through a `workspace.yaml` at repo root:
   ```yaml
   load_from:
     - grpc_server:
         host: dagster-code-server
         port: 4000
         location_name: equity_pipelines
   ```
   Bind-mounted into all three services at `/app/workspace.yaml` (image WORKDIR), NOT under `/dagster_home`. A shared named volume (`dagster_home`) with a bind-mount of a single file inside it produces a Docker mountpoint race — the first service creates the mountpoint in the volume, the second service fails with `"make mountpoint: file exists"`. Using `/app/workspace.yaml` avoids the collision because `/app` is image-content, not a volume. Webserver/daemon commands drop `-m dagster_pipelines.definitions`, replaced with `-w /app/workspace.yaml`.

3. **Replace `DefaultRunLauncher` with `DockerRunLauncher`** (`dagster.yaml` `run_launcher` block). Each run is a separate ephemeral container:
   - Its own cgroup — per-run OOM no longer touches siblings or the daemon.
   - `supports_check_run_worker_health = True` — STARTED-orphan recovery drops from ~30 min to ~2 min.
   - Image inferred from `DAGSTER_CURRENT_IMAGE` on the code-server service.
   - `/var/run/docker.sock` bind-mounted into `dagster-daemon` so it can call the Docker API (trust boundary documented in `docs/guides/ops-runbook.md` security section).

4. **Revised `mem_limit` targets** — user code having moved out of the daemon cgroup, old sizing is no longer justified:
   - `dagster-code-server`: `2g`.
   - `dagster-daemon`: `3g → 512m`.
   - `dagster` (webserver): `2g → 1g`.

Phase 1 (code-server split + `workspace.yaml`) and Phase 2 (DockerRunLauncher) ship as two sequential commits in a single PR.

## Alternatives Considered

**Status quo — keep bumping `dagster-daemon` `mem_limit`.** Apr-21 proved 3 g isn't enough under fan-out; the next incident would force 4 g, then 5 g. VPS has 15 GiB total and existing service `mem_limit`s already sum to 14.75 GiB — we are running out of headroom in the compose file. Bumping is a local fix that never addresses the structural problem (all user-code imports under one cgroup). *Rejected — ceiling-bound.*

**Lazy-import sweep of `packages/dagster-pipelines` only.** Defer the import of pandas/yfinance/sentence-transformers until the asset actually runs. Would reduce per-subprocess cold-import cost. Compounds this work (still valuable) but does not address the STARTED-orphan launcher hole and does not isolate per-run OOM. *Spun out to QNT-118 — ships independently after QNT-116.*

**Postgres-backed run storage (replacing SQLite in `DAGSTER_HOME`).** The canonical Dagster Compose example uses Postgres for run storage. We don't need it yet — one daemon, no concurrent writes to the run-storage DB. Would add a Postgres container (~300 MB), a backup process, and a migration path. *Out of scope; revisit if we ever need multiple daemons or cross-host storage.*

**Migrate to Kubernetes + `K8sRunLauncher`.** Gives the same ephemeral-container isolation plus autoscaling. Disproportionate for a single-VPS deployment that runs 10 tickers × minute-scale workloads. *Out of scope; Kubernetes is overkill until we have a real reason to leave single-VPS.*

**Autoheal sidecar + resource-limit tightening.** [QNT-104](https://linear.app/noahwins/issue/QNT-104) owns this track. Complementary coverage split:

- **Run-worker containers (this ADR)** are covered by `DockerRunLauncher`'s `supports_check_run_worker_health = True`: Dagster's daemon polls each ephemeral run worker's Docker health state, and STARTED-orphans are reaped within ~2 minutes.
- **Long-running services (QNT-104)** — `api`, `dagster-code-server`, `dagster-daemon`, `dagster` (webserver), `litellm` — are covered by the `willfarrell/autoheal` sidecar: any service labeled `autoheal=true` whose HEALTHCHECK transitions to `unhealthy` is killed by `POST /containers/{id}/kill` and replaced via `restart: unless-stopped`.

Together, every container with a HEALTHCHECK in the prod profile has a closed-loop recovery path. The two ship independently. *Kept; not an alternative, a co-ingredient.*

## Consequences

**Easier:**

- **Per-run OOM isolation.** Run-worker OOM kills the ephemeral container, not the daemon cgroup. The Apr-21 21:13 incident signature (code-server-subprocess OOM → 180 s cascade → 8 lockstep failures) becomes impossible: no subprocess lives in a cgroup that a code-loading failure could destabilize.
- **Faster STARTED-orphan recovery.** `DockerRunLauncher.supports_check_run_worker_health = True` flips `monitor_started_run`'s per-worker health branch on. STARTED-orphan recovery: ~30 min → ~2 min. The 1800 s `max_runtime_seconds` becomes a floor for long-running ops, not the primary recovery path.
- **Sanity for `mem_limit` math.** Daemon cgroup is no longer sized to absorb N × (~360 MB user-code worker) fan-out. Daemon is scheduler + sensor tick loop + queue coordinator only — steady-state ~100–200 MiB; `512m` is generous.
- **Code-location reload isolation.** `docker restart dagster-code-server` briefly blinks the webserver's code location, then recovers automatically — daemon and run workers keep going. The pre-QNT-116 equivalent was "restart the daemon to reload code," which reset every schedule/sensor cursor in memory.
- **Matches Dagster's documented production recipe.** Future upgrades (K8s, Postgres run storage, multi-code-location) are additive rather than re-architectural.

**Harder:**

- **Three services to operate instead of two.** `make check-prod`, docker-events-notify, and the health-monitor cron all need to know about `dagster-code-server`. The bind-mount pattern (QNT-112) and shared image tag keep the delta small but it is still three things to babysit.
- **gRPC service-discovery dependency.** `workspace.yaml` hardcodes `host: dagster-code-server`, which only resolves inside the compose network. Dev machines that run `dagster dev` directly (`make dev-dagster`) are unaffected (they don't use `workspace.yaml` at all).
- **Docker socket trust boundary on the daemon.** `dagster-daemon` gets `/var/run/docker.sock` bind-mounted. Anyone with RCE inside the daemon has host-container control. Mitigation is documented in the ops-runbook security section — we already run the daemon from our own image, the container command is pinned, and the host firewall restricts inbound access.
- **Container-startup overhead per run** (~2–5 s). Negligible for our minute-scale workloads but real for any sub-second job (we have none; noting for future cost-awareness).
- **Dev-parity gap.** Dev uses `CLICKHOUSE_HOST=localhost` via SSH tunnel; prod ephemeral run-worker containers don't inherit that tunnel. Dev-smoke of Phase 2's run-worker execution is practical only against a dev-local ClickHouse (or accepted as prod-only verified). Phase 1 (webserver + daemon + code-server topology) dev-smokes cleanly because it uses the same compose network abstraction as prod.

## Revisit Triggers

Revisit this ADR if any of the following happens:

- We outgrow single-VPS — multi-host run launching would push us to `K8sRunLauncher`, revisiting the "why not Kubernetes" alternative above.
- SQLite run-storage contention appears — escalate to the Postgres-backed run storage alternative. Specific signal to watch for: `database is locked` in daemon logs (grep `docker compose logs dagster-daemon | grep -i "database is locked"`), slow UI under concurrent runs, or sensor-tick stalls attributed to run-storage locks. Post-QNT-116, the daemon + every ephemeral run-worker container share write access to `history.db` via the mounted `dagster_home` volume, which is a new concurrency axis that didn't exist under `DefaultRunLauncher` (where run workers were subprocesses of the daemon, not separate OS processes contending for the SQLite lock).
- A second code location is added (e.g., a team splits off their own pipeline package) — `workspace.yaml` already supports multiple `grpc_server` entries, but we'd need a second `dagster-code-server` service.
- Container-startup overhead becomes a bottleneck (sub-second jobs or very high run rate) — `DockerRunLauncher` may no longer be the right launcher.
- The 30-day post-deploy success criterion (zero QNT-115-signature gRPC cascades) is *not* met — we missed a failure mode; re-examine before the next ratchet.

## References

- QNT-116 (this ADR's driver) — migrate to production topology.
- QNT-118 — lazy-import sweep of `packages/dagster-pipelines` (compounds QNT-116; ships independently).
- QNT-115 — Apr-21 21:13 UTC gRPC-UNAVAILABLE cascade (the incident that prompted the topology rethink).
- QNT-114 — `run_monitoring` + chaos-test finding that `DefaultRunLauncher` doesn't support worker health checks.
- QNT-113 — `QueuedRunCoordinator(max_concurrent_runs=3)` — still in force post-QNT-116; fan-out control is launcher-independent.
- QNT-112 — bind-mount pattern for repo config files (`dagster.yaml`, now `workspace.yaml`).
- QNT-111, QNT-100 — previous `mem_limit` bumps in the reactive cycle.
- QNT-104 — autoheal sidecar + resource-limit tightening (complementary).
- Dagster docs: [Deploying Dagster using Docker Compose](https://docs.dagster.io/deployment/oss/deployment-options/docker).
- Dagster docs: [DockerRunLauncher](https://docs.dagster.io/_apidocs/libraries/dagster-docker#dagster_docker.DockerRunLauncher).
- Dagster docs: [Workspace files](https://docs.dagster.io/guides/build/projects/workspaces/workspace-yaml).
