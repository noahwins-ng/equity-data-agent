# ADR-009: News Embeddings via Qdrant Cloud Inference

**Date**: 2026-04-22
**Status**: Accepted (revised same-day from an earlier inline-compute variant — see §"Revision history")

## Context

QNT-54 introduces a news-embedding pipeline: headlines in `equity_raw.news_raw` are encoded with `sentence-transformers/all-MiniLM-L6-v2` (384-dim) and upserted into Qdrant Cloud for semantic search. The Dagster run-worker previously had a flat ~360 MB peak RSS (observed in QNT-115) and the daemon cgroup is capped at 3 GB (QNT-115). Where embedding compute lives is load-bearing for that budget.

## Decision

**Use Qdrant Cloud Inference for embedding.** The Dagster asset sends raw headline text to Qdrant; Qdrant encodes server-side and stores the vector. No embedding model, no extra memory, no concurrency cap needed on the dagster-daemon side.

Concretely:

- `QdrantClient` is constructed with `cloud_inference=True`.
- Each `PointStruct.vector` is a `Document(text=headline, model="sentence-transformers/all-minilm-l6-v2")` instead of a pre-computed float list.
- The `news_embeddings` asset stays inline in the dagster run-worker subprocess, but is now I/O-bound (HTTP POST to Qdrant) instead of CPU/memory-bound.
- No `dagster/embedding` tag_concurrency_limits rule; the asset fans out alongside everything else under `max_concurrent_runs: 3` (QNT-113).
- No model weights in the `dagster` Docker image. No HuggingFace Hub dependency at build or runtime.

Free-tier budget: 5M tokens/month/model (Apr-22 Qdrant pricing). Projected load ≈ 10 tickers × ~30 headlines/tick × 6 ticks/day × 30 days × ~15 tokens/headline ≈ 810K tokens/month, ~16% of the free ceiling. Storage budget (separate) remains 1 GB → ~180 MB/year, unchanged.

## Alternatives Considered

**Inline compute on the run-worker with a `dagster/embedding: 1` tag_concurrency_limits rule (the original decision in this ADR's first draft).**
- Keeps embedding local, no vendor coupling to Qdrant's inference service.
- Costs ~500 MB resident memory per run and forces serialization of embed runs to 1 concurrent to stay inside the 3 GB cgroup (sizing math in the revision-history section below).
- Costs ~80 MB image size for baked model weights + a HuggingFace Hub dependency at image build time.
- Invalidated when we discovered Qdrant Cloud Inference already provides the exact model (`all-MiniLM-L6-v2`) on the free tier. The inline-compute constraints were solving a problem that no longer existed.

**Separate embedding microservice (FastAPI container with `/embed` endpoint).**
- Adds a 1.5 GB service to a host whose container mem_limits already sum to 14.75 / 15 GB. Requires shrinking ClickHouse to make room.
- Adds a container to monitor, a health check, a new cross-service failure mode.
- Strictly more infrastructure than any scenario justifies while Cloud Inference is free at our volume.

**Piggyback on the api service (FastAPI `/embed` endpoint).**
- Couples embedding batch work with user-facing request latency.
- Breaks the three-role architecture from ADR-003 (FastAPI = interpreter, not ML inference host).
- Rejected on architectural grounds.

**Run a self-hosted embedding model on-host without Dagster gating.**
- Pushes the daemon cgroup to ~3.24 GB peak at 3-way fan-out — re-opens the Apr 20/21 OOM failure mode.
- Rejected because "bump the mem_limit again" is the wrong pattern.

## Consequences

**Easier:**
- Zero memory cost for embedding in the Dagster container. Peak RSS stays at the QNT-115 baseline; no `mem_limit` bump, no tag-concurrency rule, no new knob to tune.
- No Docker image weight bloat (~80 MB stays out). Faster CD builds.
- Tests stay local: a fake `QdrantClient` that records `(text, model)` tuples is simpler than a fake model + fake client (Phase 3 retro lesson, applied).
- Zero coupling to sentence-transformers or PyTorch in the `dagster-pipelines` dependency graph.
- Runtime: no model-load cold start; first embed on a fresh run-worker completes in one HTTP round-trip (~hundreds of ms) instead of ~10–20 s of model load.

**Harder:**
- New runtime dependency on Qdrant Cloud Inference uptime. If Qdrant Cloud Inference is down, embedding stops — the op-retry on `news_embeddings` absorbs a brief outage but a sustained one stalls the pipeline. Same class of risk we already take on ClickHouse via Dagster.
- Egress: headline text (~2–5 KB/headline × 30 headlines/tick × 6 ticks/day × 10 tickers ≈ 0.9 MB/day) leaves our network to Qdrant. Small by any metric but worth noting for future capacity planning.
- Token budget: 810K / 5M tokens/month at current volume is ~16%. Triples if we extend to `headline + body`; if we ever pay, it's $0.02/M tokens (check at commit time) so not a blocker. Track if the monthly usage creeps above 50%.
- Model choice is now governed by Qdrant's catalog. If the upstream sentence-transformers model diverges from what Qdrant hosts, we lose a degree of freedom. Mitigation: check the "Cost: Free" list on Qdrant's inference page before adding a second model (reranker, etc.).
- Input text is sent to Qdrant's inference service. Qdrant docs say "the input used for inference is not stored" unless explicitly included in the payload — acceptable for public RSS headlines, re-evaluate if we ever embed anything private.

## Revisit Triggers

Revisit this ADR if any of the following happens:

- Qdrant Cloud Inference retires `all-MiniLM-L6-v2` or moves it off the free tier, or our monthly token usage crosses 50% of the free allowance.
- A second ML model is added that Qdrant does not host (reranker, cross-encoder, fine-tuned embedder) — the shared-infrastructure argument weakens.
- Egress volume grows >100× (e.g. embedding full article bodies at sub-minute cadence) — a self-hosted model may become cheaper than the egress + latency cost.
- A compliance/data-residency requirement appears that forbids sending headline text to a US-region third-party inference service.
- Qdrant Cloud Inference latency becomes a problem — each asset run adds one network hop per headline; at very high volume this caps throughput.

## Revision history

### Apr-22 2026 — superseded inline-compute decision

The original draft of this ADR landed the opposite decision: **run embedding inline in the run-worker, gate with `tag_concurrency_limits: dagster/embedding: 1`, bake weights into the `dagster` image**. That draft existed because, at draft time, Qdrant Cloud Inference was not yet known to the repo. Once confirmed on the Qdrant platform page (Apr-22, same day), the inline-compute constraints — the tag rule, the Dockerfile bake, the `sentence-transformers` dep, the `EmbeddingResource` — were all solving a problem that Qdrant was already solving for free.

For future-me / future-reader: the inline-compute sizing math was

```
peak_memory = 660 MB daemon baseline + N_workers × (360 MB base + 500 MB model resident)
            = 660 + 1 × 860 = ~1.52 GB at N=1 (serialized via tag rule)
            = 660 + 3 × 860 = ~3.24 GB at N=3 (OOM — why the tag rule was needed)
```

Those numbers are only relevant if we ever pivot back to self-hosted embedding, e.g. under a revisit-trigger. They are preserved here so the revisit is a known-cost decision rather than a rediscovery.

Lesson captured (memory: `feedback_calibration_window`): the cost of checking the vendor's feature surface *before* writing the inline-compute design would have been one search query, and would have saved roughly 200 LOC of code + an ADR draft. This ADR stands as an example of a same-day pivot caught while the context was still fresh.

### Apr-28 2026 — verified free-tier numbers + Workaround A escape hatch

Post-QNT-141 (Finnhub migration) + alongside QNT-142 (delta-only embedding fix). Verified the Qdrant Cloud free-tier numbers and pre-sized the local-embedding escape hatch so a future maintainer can swap under pressure without re-researching.

**Confirmed free-tier limits** (sources: <https://qdrant.tech/pricing>, <https://qdrant.tech/documentation/cloud-pricing-payments/>):

- Storage: 4 GB disk / 1 GB RAM
- Cloud inference: 5M tokens/month per model (consistent with this ADR's original budget)
- Vector count cap and per-request rate limits: not published; treat as best-effort
- Failure mode at limit: not documented publicly. Assume hard reject + paid-tier upgrade prompt rather than silent degradation, but verify empirically before relying on either.

**Where we sit (post-QNT-141 + QNT-142):** ~810K tokens/month projected (16% of free-tier budget), matching the original calibration. Pre-QNT-142, the QNT-141 Finnhub backfill briefly threatened ~93× that load (28 211 rows × 6 ticks/day × `fetched_at` full-refresh window). QNT-142's `published_at` window + delta-only upsert restores the original sizing so the free tier stays comfortable.

#### Workaround A — switch to local CPU embedding (escape hatch)

This ADR keeps cloud inference. Workaround A is the documented path *if* a trigger fires; we do not deploy it pre-emptively.

**Triggers to deploy:**

- Qdrant moves `sentence-transformers/all-MiniLM-L6-v2` off the free-tier inference list.
- Sustained consumption crosses 50% of the 5M tokens/month budget over a 30-day window.
- Qdrant's free-tier cloud inference is rate-limited or hard-rejected in production (currently unobserved; QNT-142's reduction makes this much less likely).

**Files to change** (verified 2026-04-28; line numbers approximate):

- `packages/dagster-pipelines/src/dagster_pipelines/resources/qdrant.py` (~line 72): set `cloud_inference=False`, add an optional encoder field on the resource so dependent assets can pull the same model instance.
- `packages/dagster-pipelines/src/dagster_pipelines/assets/news_embeddings.py` (~line 135): replace `vector=Document(text=..., model=EMBED_MODEL)` with `vector=encoder.encode(text).tolist()`. `PointStruct` accepts both shapes — no protocol or wire-format change.
- `packages/dagster-pipelines/pyproject.toml`: add `sentence-transformers>=3.0` (~500 MB on disk including model weights) + `torch-cpu>=2.0` (~150 MB for the CPU-only build).
- `packages/api/src/api/routers/search.py` (~line 77): same encoder swap for query-time consistency. The agent's semantic search must use the same model that produced the indexed vectors.

**Cost of Workaround A:**

- Docker image bloat: ~650 MB total (sentence-transformers package + torch-cpu + model weights).
- Run-worker peak RSS: ~360 MB → ~600 MB. Still inside QNT-115's 3 GB cgroup with the 3-concurrent-runs cap (`660 MB daemon + 3 × 600 MB ≈ 2.46 GB`). Margin is tighter than the inline-compute math from the Apr-22 superseded decision because we're at 600 MB per worker, not 860 MB.
- First-call cold-start: ~10-20 s loading the model into resident memory per worker. Sensors that fan out 10 partitions back-to-back pay the cold-start cost ~3 times (one per concurrent worker; subsequent partitions on the same worker reuse the loaded model).
- Per-article inference: ~5-10 ms on a 2-core CPU. No rate limits.

**Effect:**

- Zero token-budget concern. Qdrant becomes a pure vector DB; we own embedding compute.
- This ADR's "thin run worker" property is lost — that's the explicit trade-off accepted only if the inference budget becomes the binding constraint.
- The asset's behavioural contract (idempotent upsert keyed on `point_id(ticker, url_id)`) is unchanged; existing QNT-93 asset checks continue to pass.
- The Apr-22 same-day-pivot lesson stands: this is the same code, in the same place, that the original draft of this ADR proposed before the Qdrant Cloud Inference page was found. We keep the simpler path until a real trigger fires; if it does, the swap is bounded and well-understood.

## References

- QNT-54 — Dagster asset: news embeddings → Qdrant (this ADR's driver)
- QNT-113 — max_concurrent_runs: 3 (origin of the QueuedRunCoordinator pattern)
- QNT-114 — tag_concurrency_limits proved out on `dagster/backfill`
- QNT-115 — daemon mem_limit 2g → 3g and the 360 MB per-worker peak observation
- ADR-003 — Intelligence vs Math (three-role architecture, used to reject the api-piggyback option)
- Qdrant Cloud Inference docs: https://qdrant.tech/documentation/cloud/inference/
