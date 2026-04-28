# ADR-015: News source (Finnhub `/company-news`) + sentiment topology (async downstream classifier asset)

**Date**: 2026-04-27
**Status**: Accepted

## Context

Phase 6 (TERMINAL/NINE — `docs/design-frontend-plan.md`) wires consumer pages against a news pipeline whose final shape isn't yet decided. Two consumer tickets — QNT-131 (`pending` sentiment chip schema) and QNT-132 (`/api/v1/health` provenance strip) — both carry "blocked on news-source decision" disclaimers; QNT-72 (dashboard cards) and QNT-73 (ticker detail) pull provenance + sentiment downstream of those. Land the consumer pages before this ADR and the news cards + provenance strip get wired against an assumed shape, then need a second pass when the real shape lands. ADR-014 made the same "decide before the first commit" call for rendering modes; this ADR is the data-side counterpart.

The decision is binary on two axes:

1. **News source.** Yahoo Finance RSS (current) vs Finnhub `/company-news` vs Alpha Vantage `NEWS_SENTIMENT`.
2. **Sentiment topology.** Three options for where sentiment classification lives:
   - **(a) Async downstream Dagster asset.** `news_raw` lands rows with `sentiment_label='pending'`; a separate `news_sentiment` asset reads pending rows, classifies, and updates them in place. The `pending` window is observable in the UI.
   - **(b) In-line dependency.** `news_raw` blocks on the classifier in the same op; rows only land already-classified. No `pending` window.
   - **(c) Sentiment ships with the news payload.** No classifier asset at all; the source provides per-article and per-ticker sentiment in the response.

Topology (c) is only viable if the chosen source returns sentiment, which narrows the joint decision: AV → (c) is cheapest but caps daily volume; Finnhub → (a) or (b) because Finnhub's own `/news-sentiment` endpoint is paid (verified — `"premium":"Premium Access Required"` in the docs JSON for that endpoint, vs `"premium":null` for `/company-news`).

Phase 5 lessons that apply here:

- `feedback_quality_before_capacity_in_fallback.md` — pick the news source on quality first (per-ticker coverage, signal-to-noise), capacity second. The QNT-129 Qwen → Llama-4-Scout flip was the cost of inverting that order.
- `feedback_pre_design_cross_store_identity.md` — write upstream-PK → downstream-PK and the one-sentence invariant before bridging two stores. Same logic applies to `news source row → sentiment classifier output → ClickHouse row`.
- `feedback_verify_vendor_tiers.md` — parse vendor docs for embedded `premium` markers or probe with a demo key before declaring an endpoint paid. Both done below.
- `feedback_vendor_prod_docs.md` — read the vendor's production / rate-limit page before shipping, not after the QNT-100→116-style ratchet.

### Vendor probes (2026-04-27)

| Endpoint | Verification | Result |
|---|---|---|
| Finnhub `/company-news` | Scraped docs JSON for the `/company-news` schema object | `"premium": null`, `"freeTier": "1 year of historical news and new updates"`. Free, NA-only, 1y backfill explicit. |
| Finnhub `/news-sentiment` | Same scrape | `"premium": "Premium Access Required"`. Off the table. |
| Alpha Vantage `NEWS_SENTIMENT` | `curl …apikey=demo&tickers=AAPL` | HTTP 200, 50-item response, full schema present (`overall_sentiment_label`, `overall_sentiment_score`, `ticker_sentiment[]` with per-ticker `relevance_score` + `ticker_sentiment_label`, `source`, `source_domain`, `time_published`, `summary`, `banner_image`, `topics[]`). Sentiment ships in payload. |
| Alpha Vantage free-tier rate limit | Vendor docs + corroborating reports | **25 calls/day**, 5 RPM. Confirmed against multiple 2026 sources. |

### Capacity math

| Cadence | 10-ticker calls/day | Fits Finnhub (60 RPM)? | Fits AV (25/day)? |
|---|---|---|---|
| 4h (current) | 60 | Yes (~1% of minute budget) | **No** — 60 > 25/day ceiling |
| 6h | 40 | Yes | No |
| 12h | 20 | Yes | Yes (at the cap, no headroom) |
| 24h | 10 | Yes | Yes (<half cap) |

AV forces a cadence cut from 4h to 24h *or* a multi-ticker batched query, neither of which the 5-question coverage probe in `docs/design-frontend-plan.md` justified — and 24h cadence breaks the "near-real-time news chip" framing in design v2. Finnhub fits at any cadence we'd plausibly pick.

## Decision

**News source: Finnhub `/company-news`.** **Sentiment topology: (a) async downstream Dagster asset.** **Classifier: Groq Llama 3.3 70B** via the existing LiteLLM proxy (`equity-agent/default`, per ADR-011). **Window: classify within 24h of ingest** (asset check fires when a `pending` row exceeds the window).

QNT-131 status: **kept and required.** The `pending` window is observable under topology (a); the `pend` chip in design v2 is a legitimate UI signal, not dead pixels.
QNT-132 `sentiment` provenance value: **unblocked**, committed value `{ "model": "Llama 3.3 70B", "provider": "Groq" }`.
Ingest cadence: **keep 4h** (`news_raw_schedule` in `packages/dagster-pipelines/src/dagster_pipelines/schedules.py`). The initial cutover from Yahoo RSS does a one-time 1-year backfill (Finnhub free tier explicitly allows this).

### Topology (with cross-store identity, per `feedback_pre_design_cross_store_identity.md`)

```
+-----------------+     +--------------------+     +------------------------+
| Finnhub         | --> | news_raw (CH)      | --> | news_sentiment (CH)    |
| /company-news   |     | sentiment_label =  |     | reads pending rows,    |
| symbol={ticker} |     |   'pending' on ins.|     | calls Groq Llama 3.3,  |
+-----------------+     | publisher_name,    |     | UPDATEs in place via   |
                        | image_url added    |     | ReplacingMergeTree     |
                        +--------------------+     +------------------------+
                                  |                            |
                                  v                            v
                        +--------------------+     +------------------------+
                        | news_embeddings    |     | sentiment_label flips  |
                        | (Qdrant, ADR-009)  |     | pending -> pos/neu/neg |
                        +--------------------+     +------------------------+
```

| Boundary | Upstream PK | Downstream PK | Invariant |
|---|---|---|---|
| Finnhub → `news_raw` | Finnhub article (`url`) | `(ticker, published_at, id)` where `id = blake2b(url)` | One row per `(ticker, url)`; cross-mentioned URL → N rows (matches QNT-120 namespacing). |
| `news_raw` → `news_sentiment` | `(ticker, published_at, id)` | Same `(ticker, published_at, id)`, `sentiment_label` enum updated | The full ORDER-BY composite key (per migration 005) must round-trip — re-classification supplies the original `published_at` so the merge picks the same row. ReplacingMergeTree on `fetched_at` then keeps the latest sentiment write per row. Idempotent on re-classification. |
| `news_raw` → Qdrant `equity_news` | `(ticker, id)` | `point_id = blake2b(f"{ticker}:{id}")` | Per QNT-120 fix; unchanged by this ADR. |

### Schema deltas (out-of-scope to implement here — separate ticket)

`equity_raw.news_raw` adds three columns:

- `sentiment_label LowCardinality(String) DEFAULT 'pending'` — enum `{pending, positive, neutral, negative}`.
- `publisher_name String DEFAULT ''` — Finnhub `source` field.
- `image_url String DEFAULT ''` — Finnhub `image` field.

ReplacingMergeTree dedup key (`ticker, published_at, id`) is unchanged. The `fetched_at` version column is unchanged. Sentiment updates are writes of the same key with a new `fetched_at`; the existing dedup logic absorbs them.

### Asset checks (extend QNT-93)

- `news_sentiment_pending_age` — `MAX(now() - fetched_at)` over rows where `sentiment_label='pending'` ≤ 24h. Catches stuck classifier (Groq 429 / LiteLLM down).
- `news_sentiment_label_distribution` — `pending` fraction over the last 7d ≤ 20%. Catches a classifier that ran but silently mis-emitted (returned `pending` for everything).

### Provenance strip (QNT-132)

`/api/v1/health` `provenance` block is unblocked:

```json
"provenance": {
  "sources": ["yfinance", "Finnhub", "Qdrant"],
  "jobs": {"runtime": "Dagster", "schedule": "daily", "next_ingest_local": "02:00 ET"},
  "sentiment": {"model": "Llama 3.3 70B", "provider": "Groq"},
  "agent": {"runtime": "LangGraph"}
}
```

Source values are pulled from `shared.settings` / Dagster schedule introspection per QNT-132 scope; the model + provider come from the active LiteLLM `equity-agent/default` alias (today: Groq Llama 3.3 70B per ADR-011 §"Default"). If ADR-011 ever flips the default, this strip updates without a frontend deploy.

## Alternatives Considered

**Stay on Yahoo Finance RSS (status quo).** Already wired (`packages/dagster-pipelines/src/dagster_pipelines/news_feeds.py` → `YAHOO_TICKER_FEED`), unrate-limited, no API key. Three blockers:

1. No publisher diversity in the response shape — `source` is a free-form string, often "Yahoo Finance" rather than the originating outlet (Reuters/Bloomberg/etc). The design v2 mock wants per-publisher chips.
2. No article images. Design v2 reserves space for them; degrading to text-only is acceptable but loses density vs Finnhub.
3. RSS feeds are scoped to "what Yahoo decides to surface today" — no historical backfill, no time-range query. The 1y backfill that Finnhub allows is unreachable from RSS at any rate limit.

These are scope-side losses, not capacity ones. Rejected because the upgrade is free-tier-cheap.

**Alpha Vantage `NEWS_SENTIMENT` (topology (c) — sentiment in payload).** Tempting because the response includes per-article + per-ticker sentiment for free, removing the classifier asset entirely. The 25-call/day ceiling is the disqualifier: 10 tickers × 6 ticks/day = 60 calls, 2.4× the budget. Workarounds:

- **Comma-separated ticker list** (AV documents `&tickers=A,B,C`) → 1 call per tick = 6 calls/day — fits. But the response is then keyed by *article*, not by ticker; tickers appear inside `ticker_sentiment[]`. We'd need to fan rows out client-side and risk a relevance_score < 0.5 filter being wrong for our use case.
- **Cut cadence to 24h** → fits at 10 calls/day. Breaks the "fresh news" framing in design v2 and cuts intra-day-event coverage (NVDA earnings AH, JPM macro print AM).

Even with the multi-ticker workaround, AV pins us to a single vendor for both ingestion *and* sentiment — a vendor-down day takes both surfaces dark simultaneously. Finnhub + Groq splits the failure modes across two providers. Rejected on capacity + concentration risk; revisit if we ever want a sentiment cross-check signal alongside the Groq classifier.

**Finnhub `/news-sentiment` (Finnhub's own ticker-level sentiment aggregate, hypothetical topology (c) on Finnhub).** Probed — `"premium":"Premium Access Required"` in the docs JSON. Paid only. Rejected on the "free to clone" constraint per ADR-011.

**Topology (b) — in-line classifier dependency.** `news_raw` op calls Groq inside the same materialization, only writes already-classified rows. Simpler mental model, no `pending` window, QNT-131 closes. Rejected on failure coupling: a Groq 429 (or Groq tier change, or LiteLLM proxy down) takes down headline ingestion entirely. Topology (a) decouples — headlines land regardless, sentiment catches up on the next classifier tick. The `pend` chip is a legitimate signal of that decoupling, not an artefact to design around.

**Topology (a) with a different classifier (Gemini 2.5 Flash, FinBERT local).** Both are valid swaps under the existing LiteLLM indirection — the ADR-011 fallback chain (Llama-3.3 → Llama-4-Scout) already proves the swap path. Picking the *current ADR-011 default* (Groq Llama 3.3 70B) keeps the agent's reasoning model and the news classifier on the same provider/model so QNT-67's eval harness exercises one quality surface, not two. If Groq's daily TPD becomes a constraint with the classifier added, ADR-011's revisit triggers fire and the classifier tracks the new default — no ADR-015 amendment needed.

**Five-class sentiment (Bearish / Somewhat-Bearish / Neutral / Somewhat-Bullish / Bullish — AV's scheme).** Design v2 uses three classes (POS / NEU / NEG) plus `pending`. Three is enough for a chip; five buys nothing for our reader and adds two ambiguous boundary cases per article. Rejected on UI scope.

## Anti-patterns

These are the specific traps this ADR prevents — name them so a future contributor recognises the smell before re-introducing one:

1. **Calling Groq from inside the `news_raw` op.** That collapses topology (a) → (b). The classifier belongs in a separate `news_sentiment` asset with its own retry policy. If you find yourself adding `from litellm import completion` to `assets/news_raw.py`, stop — you've crossed the topology boundary.

2. **Hiding the `pend` chip "until classification finishes".** Topology (a)'s whole point is that the `pending` state is observable. Hiding it makes the UI lie about the system's actual state and removes the asset-check signal that the classifier is healthy. Render `pend` per design v2.

3. **Using AV's `relevance_score` as a hidden filter.** If we ever add AV as a sentiment cross-check, do not silently drop articles with `relevance_score < 0.5`; that's a quality choice that must be explicit in the asset and visible in the asset check.

4. **Backfilling 1y of news in a single materialization run.** Finnhub allows it, but a single run that does 10 tickers × 365 days is a 10× memory + time amplifier vs steady-state. The cutover ticket should chunk by month or by ticker partition; the 4h schedule then carries forward.

5. **Updating sentiment by deleting + re-inserting rows.** ReplacingMergeTree dedups on `ORDER BY (ticker, published_at, id)` with `fetched_at` as the version column. Re-insert with the same key + a fresh `fetched_at` and the merge picks the latest. Manual `ALTER TABLE … DELETE` is the wrong tool and breaks idempotency.

6. **Hardcoding "Llama 3.3 70B" in `/api/v1/health`.** The provenance strip must read from the same canonical source as the classifier asset itself — today that source is the active LiteLLM `equity-agent/default` alias in `litellm_config.yaml`. QNT-132's implementation may add a thin `shared.settings` accessor to read it (or parse the YAML directly); either way the value comes from one place. ADR-011 has already flipped the default once (Pro → Flash → Llama-3.3 → fallback chain); QNT-132 must not require an ADR amendment to track it.

## Consequences

**Easier:**

- **Phase 6 consumer pages can ship without re-work.** QNT-72 / QNT-73 wire against a committed schema; QNT-131 / QNT-132 acceptance criteria are unblocked the moment this ADR lands.
- **Cleaner provenance story.** Two providers (Finnhub for headlines, Groq for sentiment) means a single-vendor outage degrades gracefully — headlines without sentiment, or sentiment-stale headlines, instead of full UI dark.
- **Per-ticker quality.** Finnhub `source` is the originating publisher (Reuters, Bloomberg, CNBC, etc.) rather than Yahoo's aggregated "Yahoo Finance" string, so design v2's per-publisher chip becomes meaningful.
- **1y backfill on cutover.** First-run history makes the search index (Qdrant `equity_news`) immediately useful; demos don't have to wait a week for headlines to accumulate.
- **Eval harness reuses the agent model.** QNT-67's golden-set sweeps already exercise Groq Llama 3.3 70B as the agent default; adding it as the classifier means every routing change to the agent (ADR-011 revisits) automatically updates the classifier, without a second eval surface to maintain.

**Harder:**

- **Two API keys to manage instead of zero.** `FINNHUB_API_KEY` (already speccable as free-tier) joins `GROQ_API_KEY` + `GEMINI_API_KEY` from ADR-011. SOPS handles prod (QNT-102); `.env.example` adds one line. ADR-015's revisit triggers include "Finnhub key churn".
- **`pending` rows visible to readers during normal operation.** The asset check (`news_sentiment_pending_age` ≤ 24h) bounds the worst case; design v2 already accounts for the `pend` chip. Acceptable.
- **Classifier cost lives inside the Groq free-tier TPD bucket** that ADR-011 / QNT-128 already split between agent + fallback. Sentiment classification is short-prompt / short-output, but adds N=10 tickers × ~30 articles/day = ~300 classifications/day. At ~200 tokens per classification (prompt + label), that's ~60K TPD against Groq's free-tier Llama-3.3-70B daily ceiling (~100K TPD per ADR-011 §"Revision history" 2026-04-25 — verify against the live Groq dashboard at cutover-ticket time, since vendor tier numbers drift). Tight but workable; the QNT-128 fallback chain (Llama-4-Scout, 500K TPD) absorbs overflow without surfacing to the agent. Revisit if the eval harness growth + classifier load together blow the bucket.
- **`equity_raw.news_raw` schema migration is required.** Adding `sentiment_label` / `publisher_name` / `image_url` is a new migration file (`migrations/012_*.sql`). The existing Yahoo RSS rows survive; their `sentiment_label` defaults to `pending` and the classifier picks them up on first run, so the search index isn't bisected at cutover.
- **Yahoo RSS code path is dead but not deleted yet.** `news_feeds.py` and the RSS-reading branch of `news_raw.py` get a `# legacy: replaced by Finnhub fetch in QNT-XXX` comment until the cutover ticket lands. Per `feedback_calibration_window.md`, don't delete until the new path has run for a week without falling back.

## Revisit triggers

Reopen this ADR if any of these fire:

- Finnhub `/company-news` flips to `"premium":"Premium Access Required"` (or any premium marker) — re-verify with the docs scrape that pinned this ADR.
- Finnhub free-tier daily/per-minute ceiling becomes binding (today: 60 RPM, 1y backfill free; we use ~60 calls/day steady-state, ~3650 for one ticker's 1y backfill).
- Per-ticker headline density on Finnhub drops below ~3/day for any portfolio ticker over a month — design v2's news cards start emptying out; consider AV multi-ticker as a supplement.
- Groq Llama 3.3 70B's classifier accuracy on the held-out 50-headline set drops below 80% (verification target in `docs/design-frontend-plan.md`) — flip to Gemini Flash or FinBERT local, no ADR amendment required if the swap is one LiteLLM YAML edit.
- ADR-011's default model changes — QNT-132's provenance value tracks it automatically; this ADR's "classifier:" line becomes stale text, not a lie.
- `pending` rows older than 24h exceed 1% of the 7d window for two consecutive days — the topology decoupling is masking a real classifier failure.
- Sentiment-label distribution skews >70% to one class for a week — either the classifier is broken or the prompt is biased; re-check before assuming the world changed.
- We add a paid news-data line item — AV's premium tier or Finnhub's premium endpoints become reachable; cross-check sentiment becomes a layerable addition rather than a swap.

## References

- ADR-009 — Embedding via Qdrant Cloud Inference (the downstream of `news_raw` whose key/identity invariant is reused here).
- ADR-011 — LLM routing (the source of the Groq Llama 3.3 70B classifier choice and its fallback chain).
- ADR-014 — Next.js rendering mode per page (the frontend-side "decide before the first commit" counterpart).
- `docs/design-frontend-plan.md` — Phase 6 design that drives the `pending` chip + provenance strip + per-publisher attribution.
- `feedback_pre_design_cross_store_identity.md` — cross-store identity heuristic used in the "Topology" table above.
- `feedback_quality_before_capacity_in_fallback.md` — order-of-operations for the news-source pick.
- `feedback_verify_vendor_tiers.md` — vendor-tier empirical verification (the docs scrape + demo probe in §"Vendor probes").
- `feedback_milestone_demand_not_code.md` — explains why this ADR sits in Phase 6 (the demanding phase) not Phase 4 (the phase whose code it touches).
- QNT-131 — `pending` sentiment state schema + classifier output (kept and required under topology (a)).
- QNT-132 — `/api/v1/health` provenance strip (unblocked: classifier model + provider committed here).
- QNT-72 / QNT-73 — Phase 6 consumer pages that should land *after* this ADR.
- QNT-93 — news asset checks; gets two new entries (`news_sentiment_pending_age`, `news_sentiment_label_distribution`).
- QNT-120 — Qdrant point ID namespacing (the cross-store identity precedent reused above).
- QNT-128 / QNT-129 / QNT-138 — Groq fallback chain that absorbs classifier load overflow.

## Revision history

**2026-04-28 (QNT-143, ingest cadence):** "Ingest cadence: keep 4h" (§Decision) is **superseded** — `news_raw_schedule` shifted to `0 2 * * *` (02:00 ET daily, 7 days/week). The 4h cadence was inherited from QNT-53's RSS design when "near-real-time news chip" was a load-bearing demo claim; design v2 (`docs/design-frontend-plan.md` push-back #1) reframes the project as an analyst workstation with explicit `EOD · 02:00 ET` framing, so the only sub-daily schedule in the project quietly contradicted that. Post-QNT-142 the 4h cadence was functionally cheap (delta-only upsert), but cheap-and-aligned beats cheap-and-vestigial. Inference token budget drops from ~270k/mo (5%) to ~70k/mo (1.4%); Dagster runs/day from 60 to 10. The capacity table in §"Capacity math" is unaffected — Finnhub fits at any cadence we'd plausibly pick. The "Backfilling 1y of news in a single materialization run" anti-pattern (§Anti-patterns #4) now reads "the daily schedule then carries forward."

**2026-04-28 (post-QNT-141, alongside QNT-142):** Topology (a) is preserved on paper, but the classifier itself is deferred — QNT-131 moved out of Phase 6 to Backlog. Three forces drove the call:

1. **Heavy LLM use for a 3-class label** couples the classifier to the agent's Groq TPD bucket (calibrated in ADR-011 §"Free-tier budget") and inherits its rate-limit failure modes. QNT-142's Qdrant calibration trap surfaced the same shape on the embedding side post-QNT-141 backfill; we don't need a second copy of that risk on the classifier side.
2. **The `pend` / POS / NEU / NEG chip is a visual nicety, not load-bearing** for the thesis. QNT-133's structured Setup / Bull Case / Bear Case / Verdict card already carries the reasoning weight that matters for the demo. The chip's marginal contribution to the portfolio narrative is small relative to the operational cost (asset + asset checks + API field + UI component + Groq budget coupling).
3. **FinBERT (ProsusAI/finbert, local CPU inference) is the technically-cleaner direction** when/if revived — no LLM coupling, no rate limits, financial-news-tuned, three-class output matching the existing `sentiment_label` enum. The deferred QNT-131 description names FinBERT as the path; the Groq classifier path is rejected.

What stays in place from this ADR:
- News source (Finnhub `/company-news`) — shipped in QNT-141.
- `sentiment_label LowCardinality(String) DEFAULT 'pending'` column in `equity_raw.news_raw`. Existing rows remain `'pending'` indefinitely; future revisit can populate them via the FinBERT path. Removing the column would be a breaking schema change for negligible benefit.
- Cross-store identity invariant (Finnhub url → news_raw composite key → Qdrant point_id from QNT-120) — unaffected.

What changes for downstream consumers:
- **QNT-132** `provenance.sentiment` field becomes `null` (or omitted) until the classifier is revived. The field shape stays so a future revisit doesn't require a contract change.
- **QNT-72 / QNT-73** news cards drop the sentiment chip slot in v1; render headline + publisher pill + date only.
- The 24h `news_sentiment_pending_age` asset check named in §"Asset checks" is **not** added — it would WARN forever in the deferred state, which inverts its signal value. If/when QNT-131 is revived, the asset check ships with the classifier in the same PR.

The sentiment topology decision (a) — i.e., "if and when there is a classifier, run it as an async downstream Dagster asset with a `pending` window" — is unchanged. This revision defers the *implementation*, not the topology choice.
