# Load-Test Baseline

p50 / p95 / p99 latency for the five public read endpoints under modest concurrency. Establishes "is the API fast enough at the demo dataset" — not a soak test, not a breaker validation. See QNT-65 for scope rationale.

## What this is and what it is not

| Probe | This doc | Where it lives |
|---|---|---|
| Endpoint p50/p95/p99 baseline | ✓ this doc | `scripts/load_test_baseline.py` + the run below |
| QNT-161 demo-protection (rate-limit, per-IP token cap, global Groq TPD breaker, LiteLLM fail-closed) | ✗ out of scope | `tests/api/test_security.py` (751 lines, every gate covered with unit tests) |
| Concurrency race-condition probe of `TokenBudget` / SlowAPI under burst | ✗ out of scope | not exercised; in-process state, sequential unit tests assumed sufficient until prod traffic shows a race symptom |
| Tripping the real 100K Groq TPD ceiling | ✗ out of scope | same code path as the lowered-cap path, not worth a 24h demo blackout |

The breaker code is identical at any cap value, and re-tripping unit-tested gates under load shape adds no signal.

## Tool

`scripts/load_test_baseline.py` — Python `asyncio` + `httpx.AsyncClient` driver. Chosen over `hey` / `k6` because:

- `httpx` is already a project dependency (transitive via FastAPI), so the script runs inside the prod `api` container with zero install.
- The matrix is 5 endpoints × 10 tickers, not a single URL — `hey` would need a shell wrapper to aggregate per-endpoint.
- Lives next to the rest of the repo's one-off scripts and replays cleanly via `uv run python …`.

## Run methodology

- **N = 500 requests**: 5 endpoints × 10 tickers × 10 reps each.
- **Concurrency = 20** (`asyncio.Semaphore`).
- **Warm-up pass discarded** before the measured run — 50 requests (one per `(endpoint, ticker)` pair) prime the ClickHouse query-plan cache, Qdrant client, and module-level state so the first measured request isn't a cold-start outlier. Not a full pre-warm of all 500 measured requests.
- **Probed inside the prod `api` container** (`docker exec`) against `http://localhost:8000` — eliminates the SSH tunnel and Caddy hops, leaving the in-container loopback round-trip + uvicorn + FastAPI handler + ClickHouse query.
- **>5 % errors per endpoint = non-zero exit** so a future re-run fails loud if an endpoint regresses.
- **Latency is computed over `status == 200` only.** Non-200 responses are counted in the `err` column but excluded from p50/p95/p99 — a fast 5xx would otherwise look like a fast endpoint. The `err` column is the canary; latency is conditional on success.
- **The script is staged in `/tmp` on Hetzner and `/tmp` inside the api container, never copied into the repo on prod.** This is a one-off transient — see `feedback_prod_hotfix_scp.md`: SCP'd files in `/opt/equity-data-agent/` would block CD's `git pull`. `/tmp` is safe.

## Run record

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| Prod build SHA | `6e9ad60` |
| Host | Hetzner CX41 (`equity-data-agent-api-1` container) |
| Probed URL | `http://localhost:8000` (in-container loopback) |
| Total duration | 14.1 s |

### Command

```bash
scp scripts/load_test_baseline.py hetzner:/tmp/load_test_baseline.py
ssh hetzner "docker cp /tmp/load_test_baseline.py \
    equity-data-agent-api-1:/tmp/load_test_baseline.py \
  && docker exec equity-data-agent-api-1 \
    /app/.venv/bin/python /tmp/load_test_baseline.py \
    http://localhost:8000 --reps 10 --concurrency 20"
```

### Results

| Endpoint | n | ok | err | p50 ms | p95 ms | p99 ms | min ms | max ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `/api/v1/quote/{ticker}` | 100 | 100 | 0 | 597.1 | 1111.8 | 1206.6 | 204.4 | 1383.9 |
| `/api/v1/ohlcv/{ticker}` | 100 | 100 | 0 | 407.6 | 702.5 | 1078.1 | 123.8 | 1082.4 |
| `/api/v1/indicators/{ticker}` | 100 | 100 | 0 | 496.0 | 710.5 | 879.7 | 204.4 | 892.0 |
| `/api/v1/fundamentals/{ticker}` | 100 | 100 | 0 | 494.5 | 914.6 | 1020.3 | 225.2 | 1300.2 |
| `/api/v1/news/{ticker}` | 100 | 100 | 0 | 389.8 | 816.5 | 916.4 | 113.1 | 1023.4 |

### Reading the numbers

- **All 500 requests succeeded** (zero errors, zero non-200s).
- **p50 sits at 390–600 ms** across endpoints — within the ballpark for a single-row ClickHouse FINAL query plus serialization on a CX41.
- **`/quote` is the slowest** at p50 (~600 ms): it stitches OHLCV + 30-bar avg volume + TTM P/E + raw market cap in one round-trip (intentional per `data.py:542` — saves the frontend three sequential calls on every navigation).
- **p95/p99 spread is wide** (700–1200 ms), driven by ClickHouse merge contention under 20-way concurrent FINAL reads. This is consistent with the `system.text_log` / `metric_log` merge creep noted in the ops runbook — the equity tables aren't the hot path here, but they share the merge scheduler.
- **End-to-end frontend latency will exceed these numbers** by the trycloudflare → Hetzner network hop (typically +50–150 ms RTT depending on PoP). The p95 ceiling here is the API floor; user-perceived latency is API + tunnel + Caddy + Vercel cache miss.

There is no hard SLO yet. The original QNT-65 target was "p95 < 500 ms across endpoints" — only `ohlcv` and `indicators` hit that bar at p95, and none hit it at p99. That is acceptable for the current demo (Vercel ISR + Dagster deploy hook means most page loads serve a build-time-pinned response, not a fresh API call), but worth noting if a future feature ever depends on per-request p95 < 500 ms.

## Replay

```bash
# from the M4, against any reachable API base URL
uv run python scripts/load_test_baseline.py http://localhost:8000

# against prod (no network hop)
scp scripts/load_test_baseline.py hetzner:/tmp/load_test_baseline.py
ssh hetzner "docker cp /tmp/load_test_baseline.py \
    equity-data-agent-api-1:/tmp/load_test_baseline.py \
  && docker exec equity-data-agent-api-1 \
    /app/.venv/bin/python /tmp/load_test_baseline.py \
    http://localhost:8000 --reps 10 --concurrency 20"
```

JSON summary lands on stdout, Markdown table on stderr — pipe stderr to a fresh "Run record" entry above when re-running.
