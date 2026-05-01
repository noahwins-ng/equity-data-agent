# ADR-016: News publisher canonicalisation, cross-ticker storage, and dedup contract

**Date**: 2026-05-01
**Status**: Accepted

## Context

The Phase 6 ticker detail page (QNT-73) renders a per-ticker news card sourced
from `equity_raw.news_raw`. Two surface-level oddities surfaced once the card
went live:

1. **The publisher pill is wrong for ~78% of articles.** Finnhub serves
   "Yahoo"/"Benzinga"/"CNBC"/etc-tagged articles via
   `finnhub.io/api/news?id=...` redirects. The URL host is opaque
   (`finnhub.io`), and the `publisher_name` field tells us *which Finnhub
   feed* the article came from, not who actually wrote it. "Yahoo" routinely
   covers republished Reuters / Bloomberg / Fool / Barron's pieces — the
   label is the only signal we had, and it can be wrong. QNT-73 partially
   patched this by preferring `domain(url)` over `publisher_name` for
   non-`finnhub.io` URLs, which fixed the ~22% direct-outlet bucket but
   left the redirect bucket at 4,113 / 5,135 weekly rows mislabelled (per
   the cross-tab in QNT-148).

2. **Articles appear duplicated on the card.** Two unrelated mechanisms
   produce duplicates:
   - **Same-ticker, same article, multiple rows**: Finnhub occasionally
     returns the same URL across ticks with a slightly shifted `datetime`
     epoch (timezone or republish drift). The ReplacingMergeTree
     `ORDER BY (ticker, published_at, id)` only collapses *exact* key
     matches, so a 60-second timestamp drift produces N rows for the
     same article.
   - **Cross-ticker, same article**: Finnhub `/company-news?symbol=X`
     returns any article whose `related` field includes `X`. A "Big Tech
     earnings preview" piece tagged with all six mega-caps lands as six
     `(ticker, ?, id)` rows. The QNT-120 Qdrant point-id namespacing fix
     made this design intentional on the embedding side; the read path
     never spelled out the contract for the news card.

The 7-day-window cross-tab from 2026-05-01 (QNT-148 investigation):

| Metric | Value |
|---|---|
| Total rows in `news_raw` 7-day window | 5,135 |
| Distinct article ids (URL hashes) | 3,360 |
| Fanout factor (rows / distinct ids) | **1.528** |
| `finnhub.io` redirect rows | 4,113 (~80%) |
| Direct outlet rows (already-clean URL host) | 1,022 (~20%) |
| Max ticker-fanout (single article in N tickers) | 6 (covered all mega-caps) |
| Same-ticker repeat factor (max rows per `(ticker, id)`) | 3 |

The card shipping with no canonical publisher field, no resolved-host
column, and no dedup contract was a Phase 6 short-cut by design — get the
shape on screen, then come back. This ADR is the come-back.

## Decision

Three coupled decisions, all landing in QNT-148.

### 1. Publisher canonicalisation: resolve at ingest, render once

Add a `resolved_host LowCardinality(String) DEFAULT ''` column to
`equity_raw.news_raw` (migration `024`). The `news_raw` Dagster asset
populates it on every insert via `resolve_publisher_host(url)`:

```
resolve_publisher_host(url):
    if host(url) != "finnhub.io": return strip_www(host(url))   # direct outlet — short-circuit
    HEAD url, follow up to 5 redirects, 5s deadline
    if HEAD 405 / 403 / 501: retry with streamed GET (no body read)
    if non-2xx OR final host == finnhub.io OR exception: return ""
    return strip_www(host(final_url))
```

The API exposes a single canonical `publisher` field, computed once in
SQL via `multiIf` against the same priority order:

```sql
multiIf(
    resolved_host != '',                                   resolved_host,
    domain(url) NOT IN ('finnhub.io', ''),                 replaceRegexpOne(domain(url), '^www\\.', ''),
    trim(publisher_name) != '',                            trim(publisher_name),
    ''
) AS publisher
```

The frontend reads `item.publisher || "—"`. No fallback chain in the
component.

**Soft-fail contract.** Network failures during resolution store `''`
rather than crashing the asset or persisting a misleading host. The
fallback chain in the API absorbs unresolved rows: a finnhub.io row whose
HEAD timed out renders the trimmed `publisher_name` (e.g. "Yahoo")
instead of "—". Rows with neither resolved nor inferable publisher render
"—". This preserves AC #6 — no frontend regression for unresolvable
articles — and keeps the ingest run-time bounded.

**Empirical resolution rate (post-deploy 2026-05-01).** Cloudflare-fronted
Finnhub (`server: cloudflare`, `cf-cache-status: DYNAMIC`) returns
`HTTP 302 → Location: /` for the majority of redirect requests, even
with realistic browser User-Agent + Referer + 1.5s per-process rate
limit. The behavior is sticky: URLs that resolve successfully during one
asset run return `/` when probed 10–20 minutes later from the same IP.
The signal looks like per-source bot mitigation (or a sliding-window
throttle the redirect server doesn't document) rather than per-URL
expiry — recent and older articles fail at the same rate, and a
staggered one-ticker re-run gets ~44% while a parallel 10-ticker burst
gets 1–6% per ticker.

What this means for AC #4: across the rolling 7-day window, **~22% of
rows are direct outlets (resolved_host populated by short-circuit) +
~33% are successfully resolved Finnhub redirects = ~55% have a real
outlet credited**, with the remaining ~44% falling back to
`publisher_name` (the Finnhub feed-source label, "Yahoo"/"Benzinga"/etc.).
The pre-PR baseline was ~22% accurate (direct outlets only) + ~78%
showing the Finnhub feed-source label; post-PR is strictly an
improvement (the resolved-redirect rows correct their attribution; the
unresolved rows fall through to the same label they showed before). The
80% target in the original ticket was aspirational; **the achievable
ceiling without bypassing Cloudflare or switching to a different
Finnhub endpoint is ~55%**, so the AC is revised accordingly. Pushing
higher would require either (a) parsing article body for byline
(explicit out-of-scope per the ticket), (b) a paid Finnhub endpoint
that returns the original outlet URL alongside the article (unverified
pricing — separate ticket if revisited), or (c) a long-running
cookie/session strategy that survives Cloudflare's per-source budget
(speculative, fragile).

### 2. Cross-ticker storage: one row per `(ticker, id)`, no implicit merge

Cross-mentioned articles continue to land as N rows (one per ticker). The
ReplacingMergeTree key stays `(ticker, published_at, id)`, the Qdrant point
ID stays `blake2b(f"{ticker}:{id}")` (per QNT-120). Same-URL-different-ticker
is **not** a duplication bug, it's a per-ticker mention by design:

| Scenario | Storage | Surfaces under |
|---|---|---|
| AAPL-only article | 1 row | AAPL only |
| AAPL+MSFT mentioned article | 2 rows | AAPL feed *and* MSFT feed |
| Same URL, drifted `published_at` for AAPL | N rows under AAPL | dedup'd at API read time (see §3) |
| Same URL across all 6 mega-caps | 6 rows × ≤ N drift each | each ticker's feed independently |

This makes the per-ticker query the natural unit of work and matches the
mental model of "this article mentioned the ticker." It also keeps the
per-ticker filter on the Qdrant search index untouched.

### 3. Dedup at API read time, by article `id` within ticker

The `/api/v1/news/{ticker}` endpoint groups by `id` and picks the
`argMax(field, published_at)` for every payload field, with `max(published_at)`
as the row timestamp. This collapses the same-ticker-multiple-rows case
(timestamp drift) to one row per article without losing any cross-ticker
signal — each ticker's query still sees its own copy of a cross-mentioned
article.

Cross-ticker dedup is **deliberately not** done. A reader on the AAPL page
expects to see "Big Tech earnings preview" in their AAPL feed; collapsing
it into "AAPL gets it because alphabetical" would silently drop the article
from MSFT's feed. The cross-ticker view is the responsibility of a future
"Related across portfolio" surface (see §4).

**Why id, not (host + headline)**: the `id` column is `blake2b(url)`,
which is exactly the dedup signal we want — same URL is the same article
modulo aggregator paywalls. Headline-based dedup would false-negative on
"5 reasons NVDA could rally" vs "Five reasons NVDA could rally" (same
article, identical URL) and false-positive on legitimately distinct
articles with copied headlines. URL-hash is the strongest cheap signal.

### 4. Cross-ticker peer-news surfacing: deferred

QNT-148 AC #10 asks for a "Related across portfolio" affordance backed by
Qdrant peer-ticker similarity, OR an explicit deferral.

**Decision**: deferred to a later ticket, with rationale:

* The Qdrant infrastructure for cross-ticker similar-article search
  already exists (`equity_news` collection + payload `ticker` filter), so
  no infra work is blocked. Surfacing it is purely a UI ranking + placement
  decision.
* Shipping it well needs a design pass on how "related to portfolio"
  interacts with the existing watchlist sparkline strip and the
  ticker-detail-page news card layout. Both surfaces are recently shipped
  (QNT-72 / QNT-73) and still maturing.
* The marginal value of peer-news is unclear without first observing how
  readers use the de-duplicated single-ticker feed shipped in this ticket.
  Adding the affordance now risks designing against an assumption rather
  than a measured reader pattern.

Revisit trigger: after one full week of usage data on the post-QNT-148
news card, decide whether peer-news is a card-level addition (e.g.
"Mentioned alongside MSFT, GOOGL") or a separate surface.

## Cross-store identity

Following the heuristic from
`feedback_pre_design_cross_store_identity.md` — write upstream PK →
downstream PK and the one-sentence invariant before bridging two stores.

| Boundary | Upstream PK | Downstream PK | Invariant |
|---|---|---|---|
| Finnhub `/company-news?symbol=X` → `news_raw` | Finnhub article (`url`) | `(ticker, published_at, id)` where `id = blake2b(url)` | One row per (ticker, url) pair; cross-mentioned URL → N rows. Inherited from ADR-015. |
| Finnhub redirect host → `news_raw.resolved_host` | `url` (`finnhub.io/api/news?id=...`) | `(ticker, published_at, id).resolved_host` | At-most-one resolution per row — soft-fails to `''` on any error; idempotent on re-fetch (same URL → same `id` → ReplacingMergeTree absorbs the re-write). |
| `news_raw` → API `/api/v1/news/{ticker}` | `(ticker, published_at, id)` | `(ticker, id)` after `argMax` dedup | Same article URL collapses to one card row per ticker; cross-ticker rows remain independent. |
| `news_raw` → Qdrant `equity_news` | `(ticker, id)` | `point_id = blake2b(f"{ticker}:{id}")` | Unchanged from QNT-120 / ADR-015. |

## Per-ticker attribution semantics

For AC #7 — how does an article end up under a given ticker?

Finnhub's `/company-news?symbol=X` returns articles whose `related` array
includes `X`. The matching is **string-equality on the symbol**, not
semantic relevance scoring. A small-print mention of `AMZN` in a
META-focused article still puts the article in AMZN's feed; whether the
piece is *useful* to an AMZN reader is not Finnhub's call to make.

Implications we accept:
* AMZN's feed will occasionally surface articles that are 90% META and
  drop AMZN's name once. Acceptable — the alternative (relevance scoring)
  is opaque and can hide articles a reader wants.
* The mega-cap fanout (one earnings-preview piece across all six tickers)
  is by design — readers who watch the whole portfolio see the article in
  every ticker they hold; the cross-ticker dedup contract above leaves it
  to the "Related across portfolio" surface (deferred §4) to collapse.

If Finnhub ever changes the matching to relevance scoring (or adds a
threshold parameter), revisit this ADR — the per-ticker fanout numbers in
the table above will shift, and the dedup contract may need to grow a
relevance filter alongside the URL-hash check.

## Alternatives Considered

**Resolve redirects at render time** (frontend or API does HEAD on each
request). Rejected: every page render would HEAD ~25 outlets, blowing
through outlet rate limits within hours and turning the news card into a
DDOS source. Resolution belongs at ingest where it runs once per article.

**Parse outlet from article body / "By Reuters" wire-service tags.**
Rejected for now (out of scope per the QNT-148 ticket). The HEAD-resolve
path gives us the *serving* domain, which is enough to credit the right
outlet. Wire-service byline parsing is a separate, deeper problem (HTML
shape varies per outlet, syndication banners are inconsistent) and would
be a Phase 7 polish if we ever care.

**Dedup by `(host, headline)`** instead of `id`. Rejected — see §3 above.
URL-hash dedup is strictly stronger and the `id` column already exists
for this purpose.

**Collapse cross-ticker rows at the warehouse layer** (one row per
article, ticker stored as `Array(LowCardinality(String))`). Rejected:
breaks the current per-ticker `WHERE ticker = X` query plan, and
contradicts QNT-120's namespacing fix on the Qdrant side which depends on
`(ticker, id)` being a real composite. The cross-ticker dedup belongs at
the read view (when/if shipped), not the storage layer.

**Backfill `resolved_host` for historical rows in this ticket.** Rejected
for v1: the asset's 7-day lookback window naturally re-ingests every
article during the next daily run, populating `resolved_host` on the
re-write (ReplacingMergeTree dedups on the same key with the new
`fetched_at`). After ~7 daily ticks the steady-state window is fully
populated; no separate one-shot backfill is needed. Articles older than
7 days remain at `resolved_host = ''` and fall back to the API's
`multiIf` chain — acceptable since the news card is a 7-day surface.

## Consequences

**Easier:**

* The frontend pill renders one field (`item.publisher`) with no logic.
  Future formatting changes (capitalization, length truncation, prefix
  stripping) live in one SQL expression instead of N component-level
  helpers.
* Adding a debug surface ("show me the raw publisher signals") is cheap
  — `publisher_name`, `url`, and `resolved_host` are all still in the
  payload.
* Per-article correctness: ~78% of weekly rows go from "Yahoo" / "Benzinga"
  generic labels to the actual outlet (cnbc.com, seekingalpha.com,
  benzinga.com, …) once the daily cycle has populated `resolved_host`.

**Harder:**

* The asset now does up-to-N HEAD requests per partition per tick, where
  N is the per-partition article count. At ~30 articles/day/ticker × 10
  tickers = ~300 HEAD requests/day in steady state, all soft-failing on
  timeout. Worst case (full 7-day re-ingest after a missed week) is ~2k
  HEADs across all partitions; sequential per-partition with a 5s
  deadline each puts the wall-clock ceiling around 2-3 minutes per
  partition, which is well within the existing run budget.
* Two new soft-failure modes the ops runbook will eventually need to
  describe: outlet flapping (HEAD timeouts spike) and Finnhub redirect
  rewriting (the resolved host changes for the same article between
  ticks). Both are already absorbed by the soft-fail contract — no
  asset retries, no run-level alerts — but a future operator looking
  at "why did `resolved_host` drift" should know where to look.
* `equity_raw.news_raw` schema grows by one column (3 LowCardinality
  bytes per row). At the current ~30k rolling 7-day rows, the storage
  cost is negligible.

## References

* QNT-148 — implements this ADR.
* QNT-73 — Phase 6 ticker detail page; demand-driver for the canonical
  publisher field.
* QNT-120 — Qdrant point-id namespacing; the precedent for "one row per
  (ticker, id), even when the underlying URL is shared."
* QNT-141 — Yahoo-RSS → Finnhub cutover, the source of the redirect-label
  problem this ADR resolves.
* ADR-009 — Embedding via Qdrant Cloud Inference; the rolling 7-day
  window framing.
* ADR-014 — Next.js rendering mode per page; the "empty list ≡ service
  down" anti-pattern this card honours.
* ADR-015 — Finnhub `/company-news` source pick; the cross-store
  identity table this ADR extends.
* `feedback_pre_design_cross_store_identity.md` — the heuristic used in
  the §"Cross-store identity" table above.
* `feedback_milestone_demand_not_code.md` — explains why this ADR sits
  in Phase 6 (the demanding phase) not Phase 4 (the phase whose ingest
  code it touches).
