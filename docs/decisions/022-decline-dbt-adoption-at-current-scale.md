# ADR-022: Decline dbt adoption at current scale; keep the SQL-vs-Python boundary rule

**Date**: 2026-06-16
**Status**: Accepted

## Context

QNT-242 was a spike to decide whether to adopt `dagster-dbt` + `dbt-clickhouse`
for the warehouse's SQL-shaped transforms. The trigger was that dbt is the one
data-engineering standard whose *vocabulary framing* (per ADR-style README
positioning, QNT-241) does not help at the recruiter keyword-screen layer, which
runs before any human reads the README. So the question was real: is dbt worth
adopting here on engineering merit, on hiring-signal merit, or neither?

A code-grounded comparison (2026-06-13, `docs/de-improvement-v1.md` Appendix A)
examined the only migration candidates - the OHLCV aggregation assets
`ohlcv_weekly` / `ohlcv_monthly` in
`packages/dagster-pipelines/src/dagster_pipelines/assets/aggregation/ohlcv_aggregation.py`.

## Decision

**Do not adopt dbt now.** The two OHLCV aggregation assets stay as Dagster
pandas assets. No `dagster-dbt` / `dbt-clickhouse` dependency is added, no POC
branch is cut, and no follow-up implementation ticket is created. The time
allocated to QNT-242 is reallocated to the data-quality lane (QNT-243
reject/quarantine handling); the data-observability fallback (QNT-240) already
shipped.

**What we keep regardless** - the boundary rule the spike produced, which is the
durable, reusable part of this decision:

> SQL-shaped, set-based transforms belong in declarative SQL models; Python math
> (RSI / MACD / SMA and the other indicators) belongs in Dagster assets. If dbt
> is ever adopted, `dagster-dbt` stitches both into one asset graph - dbt owns
> the set-based layer, Dagster owns the imperative-math layer. Adoption is gated
> on demand (a larger SQL surface or a hiring requirement), not on taste.

This is recorded so a future "should we add dbt?" question resolves against a
written rule instead of being re-litigated from scratch.

### Why decline (the engineering case is decisive)

The comparison found **no engineering jump** at this scale. Per Appendix A:

| Dimension | Existing (Dagster pandas asset) | dagster-dbt | Net |
|---|---|---|---|
| Transform shape | ~50-line imperative pandas body x2 (+ a shared ~22-line helper) | ~10-line declarative SQL x2 | dbt better |
| Compute | full series -> worker RAM -> pandas -> reinsert | ClickHouse aggregates in place | dbt better |
| Partitioning | per-ticker `StaticPartition` (isolated retries + per-partition observability) | whole-table `table` rebuild | **lost** |
| Testing | aggregation outputs have no dedicated asset checks today; the project's check idiom (e.g. `pe_in_band` on `fundamental_summary`) already exceeds dbt's built-ins | `schema.yml` not_null/unique | **neutral** (any add is achievable natively) |
| Scope | - | only ~2 of N transforms qualify; indicators stay Python | thin slice |
| Deps / deploy | none; already in image | `dagster-dbt` + `dbt-clickhouse` + manifest-compiled-at-build-time | added surface |

The one genuine win - pushing the group-by down into ClickHouse SQL
(`toMonday` / `toStartOfMonth` + `argMin` / `argMax` + `sum`) instead of pulling
the series to a pandas worker - is real but marginal at 10 tickers, and it is
bought at the cost of losing per-ticker partition granularity and taking on a new
dependency + build-time manifest surface for a ~2-transform slice. The testing
story does not improve either: the aggregation outputs carry no dedicated asset
checks today, so dbt's stock `not_null`/`unique` would be a marginal *add* there
- but that same coverage is achievable as native Dagster asset checks (the idiom
the project already uses elsewhere, e.g. `pe_in_band`, exceeds dbt's built-ins),
so it is no reason to adopt dbt. Net: a near-even trade whose only tiebreaker was
hiring signal.

### On the hiring gate

The ticket gated adoption on a job-listing scan (proceed only if >= half of ~10
target listings require dbt). That scan was **not run**; the decision was made
directly on the engineering verdict above plus the portfolio owner's call that a
weekend is better spent on substance (QNT-243) than on a keyword-driven token
project. The reasoning: even in the best case where the gate cleared, the
defensible interview artifact is *this decision record* - "I evaluated dbt
against the existing asset graph, found it a near-even trade at this scale, and
declined with a written boundary rule" - which is a stronger signal than a
two-model POC that adds a dependency for show. The cheap-but-strong move was
always the ADR, not the build.

## Alternatives Considered

* **Adopt dbt for the two OHLCV aggregations (the full build).** Rejected: loses
  per-ticker partition granularity, no testing gain over native asset checks, and
  adds a dependency + build-time manifest for ~2 transforms. No engineering payoff
  at this scale.
* **Cut a throwaway POC branch first (gate 2), then decide.** Rejected: the
  decision is already determined by the engineering comparison; a POC would only
  confirm toolchain mechanics (`dagster-dbt` + `dbt-clickhouse` +
  manifest-at-build-time), not change the verdict. Toolchain feasibility is not
  in doubt - both are flagship/vendor-maintained integrations.
* **Run the job-listing scan and let it decide.** Reasonable, but the owner
  chose to decline directly; the scan can be re-run later if the calculus
  changes (see below). The decision is intentionally cheap to revisit.

## Consequences

**Easier**

* The dbt question is closed against a written rule; a future re-raise resolves
  against the boundary rule, not a blank slate.
* No new dependency (`dagster-dbt`, `dbt-clickhouse`), no build-time manifest
  step, no version-compat surface in the Docker image.
* The QNT-242 time goes to QNT-243 (reject/quarantine), which is data-quality
  substance rather than keyword signal.

**Harder / watch**

* The recruiter keyword-screen gap remains: a pure-ATS screen filtering on "dbt"
  will not match this repo. Accepted - the README DE positioning (QNT-241) and
  this ADR are the mitigations for the human-review layer, not the ATS layer.
* If the SQL-shaped surface grows materially (more set-based transforms than the
  current ~2) or a concrete target role lists dbt as a hard requirement, revisit
  - the migration gotcha to pin at that point is already identified: the current
  pandas code uses local `date.today()` for the incomplete-period skip while
  ClickHouse `today()` is server-tz/UTC, so any dbt port must include a test
  asserting the SQL and pandas versions produce identical period sets.

## Acceptance-criteria status

* **AC1 (decision + reasoning recorded)** - this ADR plus the decision comment on
  QNT-242. Decision: **skip / decline**. ✓ (the job-listing scan was deliberately
  not run; decided on the engineering verdict + owner call, documented above)
* **AC2 (optional POC)** - **N/A**: gate 1 did not clear, so gate 2 (POC) is not
  entered. ✓
* **AC3 (handoff)** - skip path: QNT-240 (data observability) already shipped;
  no follow-up implementation ticket is created; QNT-242 is resolved with this
  reasoning. The reallocated effort goes to QNT-243. ✓
