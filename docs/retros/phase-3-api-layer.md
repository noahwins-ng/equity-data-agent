# Retrospective: Phase 3 — API Layer

**Timeline**: 2026-04-17 → 2026-04-19 (3 days, single cycle)
**Shipped**: 11 issues across 7 PRs

---

## What shipped

**Report endpoints (text — for agent):**

| Issue | Title | PR | Merged |
|---|---|---|---|
| QNT-69 | Design report templates for LLM consumption | #53 | 2026-04-17 |
| QNT-48 | `GET /api/v1/reports/technical/{ticker}` | #53 (bundled) | 2026-04-17 |
| QNT-49 | `GET /api/v1/reports/fundamental/{ticker}` | #53 (bundled) | 2026-04-17 |
| QNT-50 | `GET /api/v1/reports/summary/{ticker}` | #53 (bundled) | 2026-04-17 |
| QNT-79 | `GET /api/v1/reports/news/{ticker}` | #53 (bundled) | 2026-04-17 |

**Data endpoints (JSON — for frontend):**

| Issue | Title | PR | Merged |
|---|---|---|---|
| QNT-76 | `GET /api/v1/ohlcv/{ticker}` | #62 | 2026-04-18 |
| QNT-77 | `GET /api/v1/indicators/{ticker}` | #63 | 2026-04-18 |
| QNT-80 | `GET /api/v1/fundamentals/{ticker}` | #64 | 2026-04-18 |
| QNT-81 | `GET /api/v1/dashboard/summary` | #65 | 2026-04-18 |

**Utility:**

| Issue | Title | PR | Merged |
|---|---|---|---|
| QNT-51 | Health endpoint + app bootstrap | #55 | 2026-04-17 |
| QNT-78 | `GET /api/v1/tickers` — ticker registry endpoint | #69 | 2026-04-19 |

**Velocity**: 11 issues, 7 PRs, 2.5 days. Single developer. One cycle.

---

## What went well

- **Template-pattern PR** (QNT-69): build the hardest report end-to-end first, then parameterise. PR #53 shipped 5 issues (template design + 4 report endpoints) in one merge. 4× CI savings versus shipping each separately, zero divergence across the four resulting templates.
- **Fake-ClickHouse test fixture**: `_FakeClient` in `packages/api/tests/test_data.py` let every data endpoint test exercise end-to-end behaviour against canned query results. 54 API tests run in 0.6s with no SSH tunnel — CI deterministic across the whole phase.
- **Ticker validation consistency**: every `{ticker}` path endpoint and every report formatter validates against `shared.tickers.TICKERS` and returns 404 on unknown. Zero bugs across 10 endpoints × 10 tickers in prod verification.
- **N/M display conventions carried forward**: every null in reports renders as `N/M (<reason>)` — never blank, never "None", never 0. Verified live (UNH P/E → `N/M (near-zero earnings)`; empty NVDA news → `N/M (no news ingested — Phase 4 pipeline pending)`).
- **CD hard gates from Phase 2 retro earned their keep**: every Phase 3 `/ship` invocation passed the QNT-88 (SHA match) and QNT-89 (Dagster asset-graph load) gates. The same pattern that would have caught the Apr 16 drift caught nothing — because nothing drifted. That's the goal.
- **`/health` exposes deploy identity** (QNT-51): runtime `git_sha` + Dagster asset/check counts → external monitoring can now distinguish "API is up" from "API is running the code we think it is".

---

## What was harder than expected

- **Apr 18 kernel-reboot outage interrupted mid-phase**: ~48min API outage → reactive Ops work (QNT-95 restart policy, QNT-96 reboot alerting) took precedence over Phase 3 deliverables. Surfaced durability gap that `/health` 200 alone couldn't catch. Fed the `/health 200 is not a durability signal` memory.
- **QNT-78's "frontend selector" AC had no verifier**: the frontend doesn't exist yet. Resolved by introducing `⏳ PENDING` as a new AC state (distinct from ✓ and ✗ BLOCKED) — the producer ships, the consumer ticket in Phase 6 inherits the verification. New `feedback_multi_phase_ac_pending.md` memory captures the rule.
- **Cross-cutting plan items drifted**: Phase 3's cross-cutting section (CORS, ticker validation, "no auth in scope") had no QNT tags and wasn't ticked when the corresponding code shipped inline elsewhere. Required a manual reconciliation pass (PR #70) at phase end. Future phases should either (a) tag every plan bullet to a Linear issue, or (b) accept they'll need explicit /sync-docs at phase end.

---

## Lessons saved to memory

- **Template-pattern PR bundling** (`feedback_template_pattern_pr.md`): when one ticket designs a reusable shape and N tickets apply it, ship as one PR end-to-end-then-parameterise with multi-`Closes` keywords. Cited: PR #53.
- **Multi-phase AC — ⏳ PENDING** (`feedback_multi_phase_ac_pending.md`): AC with a verifier in a later phase gets ⏳ PENDING, not ✓ or ✗ BLOCKED; consumer ticket inherits it. Cited: QNT-78 frontend selector → Phase 6.

---

## Phase review — scope changes applied

Cross-referenced Phase 3 lessons against Phases 4–7; actioned three scope changes via `/change-scope`:

| Phase | Action | Target | Lesson applied |
|---|---|---|---|
| 4 | modify | QNT-54, QNT-55 | Tests must use a fake Qdrant client (analogous to `_FakeClient` for ClickHouse) — CI stays tunnel-free |
| 5 | modify | QNT-57 | Ship all 5 agent tools in ONE PR using the QNT-69 template-pattern (build `search_news` end-to-end first, then parameterise). Also fixed missing `get_news_report` from Linear description. |
| 6 | modify | Phase 6 plan | Cross-cutting bullet: ticker list sourced from `GET /api/v1/tickers` on every page — never hardcoded. Inherits QNT-78's ⏳ PENDING AC. |

Phase 7 reviewed — no changes warranted. Phase 3 durability/observability lessons already absorbed into the perpetual Ops & Reliability milestone (QNT-88/89/90/95/96).

---

## System-overview review

`docs/architecture/system-overview.md` already reflects Phase 3 state — all 4 report endpoints, 4 data endpoints, 2 utility endpoints listed; CD hard gates described (lines 136); health monitoring described (lines 138); ticker-validation + no-auth cross-cutting noted (line 93). No updates needed.

---

## Next up: Phase 4 — Narrative Data

| Issue | Title | Priority | Notes |
|---|---|---|---|
| QNT-52 | Ingest news via RSS + feedparser | High | Per-ticker Yahoo Finance RSS + broad market feeds |
| QNT-53 | `news_raw` Dagster asset (RSS → ClickHouse) | High | `default_status=RUNNING`; sensor batches events per tick |
| QNT-54 | `news_embeddings` Dagster asset (→ Qdrant) | High | **Now requires fake Qdrant client for tests** |
| QNT-55 | `GET /api/v1/search/news` | High | **Now requires fake Qdrant client for tests** |
| QNT-93 | Dagster asset checks for news assets | Medium | Real domain bounds, not "not null" |

Velocity-based suggestion: Phase 3 shipped ~4 issues/day. Phase 4 has 5 issues. Pull all 5 into cycle 2. Ordering: QNT-52 → QNT-53 → QNT-93 → QNT-54 → QNT-55.

Phase 4 inherits three forward-looking items from earlier retros:
- News schedule `default_status=RUNNING` (Phase 2 — QNT-92 lesson)
- Downstream sensor batches events per tick (Phase 2 — QNT-46 lesson)
- Asset checks with real domain bounds (Phase 2 — QNT-68 lesson)
- **Fake Qdrant client for tests** (Phase 3 — QNT-69/_FakeClient lesson, applied today)
