# Retrospective: Phase 4 — Narrative Data

**Timeline**: 2026-04-20 16:07Z → 2026-04-23 12:55Z (~3 days, all within cycle 2)
**Shipped**: 6 issues, 7 PRs merged, 0 rollovers

## What shipped

| Ticket | Title | PR | Merged |
|---|---|---|---|
| QNT-52 | RSS ingestion via feedparser | #91 | 2026-04-20 16:29Z |
| QNT-53 | `news_raw` asset + 4-hour RSS schedule | #92 | 2026-04-20 17:59Z |
| QNT-54 | `news_embeddings` asset via Qdrant Cloud Inference | #98 (+ #99 creds chore) | 2026-04-21 17:19Z |
| QNT-55 | `GET /api/v1/search/news` semantic search | #102 | 2026-04-22 15:40Z |
| QNT-93 | Asset checks for `news_raw` and `news_embeddings` | #103 | 2026-04-23 11:59Z |
| QNT-120 | Namespace Qdrant point IDs by ticker | #104 + #105 | 2026-04-23 12:39Z / 12:55Z |

## What went well

- **RSS-over-paid-API scope discipline paid off.** QNT-52 shipped in 22 minutes — the scope decision ("no paid news API evaluation; RSS is deterministic enough for 10 tickers") was pre-made and ruthlessly defended. Phase-level scope was well-defended.
- **Asset checks earned their keep within 24 hours.** QNT-93 merged at 11:59Z, QNT-120 was filed at 12:19Z — 20 minutes between a domain-bounded check shipping and its first real-bug catch (cross-ticker Qdrant overwrite). Strongest possible validation of `feedback_asset_checks_catch_real_bugs.md`.
- **Template-pattern bundling held up.** `news_raw` + `news_embeddings` + asset checks for both shipped as focused single-concern PRs; QNT-57 (Phase 5 tools) and QNT-67 (evals) are already structured to reuse the pattern.
- **Calibration-window discipline.** QNT-120 was investigated and shipped same-day while the incident context was fresh. Two follow-up memories (`dont_explain_away_first_warn`, `asset_checks_match_asset_semantic`) came out of the live session, not a retro retrofit.

## What was harder than expected

- **QNT-120 shipped as two PRs.** PR #104 fixed the point-ID scheme; post-deploy verification surfaced a secondary gap — the QNT-93 `vector_count_matches_source` check counted raw `news_raw` rows but the asset dedups to one Qdrant point per `(ticker, url_id)`, so re-published articles with bumped `published_at` looked like drift. PR #105 switched the check's CH side to `uniqExact(id) GROUP BY ticker`. Two PRs is not wasted work — it showed the self-verification loop working.
- **ADR-009 covered memory, token, and egress budgets but not key identity.** The QNT-54 design thought in "one URL = one embedding" while ClickHouse's composite key was `(ticker, url)`. Gap was invisible until QNT-93 ran against prod.

## Lessons saved to memory

- **`feedback_dont_explain_away_first_warn.md`** — domain-bounded asset checks exist to catch bugs code review misses; investigate the first prod WARN with a boundary-query + sample-row check, don't reach for "backlog/transient".
- **`feedback_asset_checks_match_asset_semantic.md`** — compare counts at the aggregate the asset actually materialises (`uniqExact` on the composite key), not `count()` on the source. QNT-93's check was silently off-by-dedup until QNT-120 made it observable.
- **`feedback_pre_design_cross_store_identity.md`** — before bridging two stores, write upstream PK tuple → downstream PK tuple and the one-sentence invariant. Both beginner-architecture incidents in the project to date — Dagster tutorial topology (QNT-100 → QNT-116, ~17h over 4 incidents) and Qdrant point ID (QNT-54 → QNT-120) — are instances of the same pattern: quickstart defaults meet multi-dimensional reality, drift silently, surface at prod scale.

## Beginner-architecture pattern — forward prevention

The user's framing for this retro: *"how do we avoid such beginner Dagster architecture being applied to other aspects in next phases?"*

Pattern characterisation:
- Quickstart / tutorial defaults are single-dimensional (one partition, one key, one runner).
- Our data model is multi-dimensional (10 tickers, composite keys, fan-out runs).
- When single-dim defaults meet multi-dim reality, the failure surfaces as silent drift (QNT-120) or kernel OOMs (QNT-111/113/115/116).
- Both arcs were caught by domain-bounded checks firing on prod data — after the design had shipped.

Forward prevention applied as scope changes out of this retro:

| Phase | Change | Mechanism |
|---|---|---|
| Phase 5 | QNT-57 requires pre-implementation tool-contract block in PR body | Input schema → upstream HTTP call → return-string shape + degraded case. Forces API-response ↔ graph-state identity to surface at design time. |
| Phase 5 | QNT-67 requires ≥1 question per ticker in `shared.tickers.TICKERS` (golden-set coverage invariant) | Applies Phase 2's sample-broadly lesson to the hallucination eval harness. |
| Phase 6 | QNT-121 (new) — ADR-011 Next.js rendering mode per page written BEFORE any page code | Same pattern applied to Next.js app-router: pre-decide rendering mode, cache strategy, failure-mode rendering per page. |
| Ops | QNT-122 (new) — audit existing asset checks for composite-key aggregation correctness | `feedback_fix_pattern_not_example`: sweep for every instance of the QNT-93 off-by-dedup class, don't just fix the one QNT-120 exposed. |

## System overview updates

Refreshed `docs/architecture/system-overview.md`:
- Asset-check count 17/6 → 25/10 (QNT-93 + QNT-120 assets).
- Added QNT-120 note on Qdrant point ID namespacing by ticker.
- Removed "depends on Phase 4 data" qualifier from `/reports/news/` and `/search/news` (Phase 4 is complete).

## Next up

**Phase 5 — Agent Layer.** 10 tickets in backlog: LiteLLM proxy, Langfuse tracing (day-one), LangGraph state + graph, tools (with new contract-block discipline), CLI, eval harness (highest priority), SSE endpoint, README + demo screencast. Cycle 3 starts 2026-04-26.
