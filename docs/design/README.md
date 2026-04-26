# Design assets

Canonical visual references for the Phase 6 frontend (TERMINAL/NINE).

| File | What it captures | Date | Notes |
|---|---|---|---|
| [`v1-original.png`](v1-original.png) | First-draft mock from Claude Design | 2026-04-25 | The "trading terminal" framing — live tick chrome, FMP/Finnhub/TradingView/EDGAR aspirational footer, prestige logos, FWD column, VWAP, THESIS DOC tab. Kept as the artifact we pushed back on; useful for "why did we change X?" archaeology. |
| [`v2-final.png`](v2-final.png) | Revised mock after design assessment | 2026-04-26 | The "analyst workstation" framing — EOD chrome, real source footer (`yfinance · Finnhub · Qdrant`), text-first publisher pills, sentiment chips with `pend` placeholder, Quarterly/Annual/TTM fundamentals tabs, EBITDA margin + ROE/ROA substitutes, persistent right-pane chat with structured thesis (Setup/Bull/Bear/Verdict), data-driven provenance strip (SOURCES/JOBS/SENTIMENT/AGENT). **This is the canonical reference for QNT-72/73/74 implementation.** |

## Source-of-truth links

- Claude Design URL: *(ask user for current link)*
- Assessment doc: [`../design-frontend-plan.md`](../design-frontend-plan.md)
- Project plan Phase 6 section: [`../project-plan.md`](../project-plan.md)

## Convention for future iterations

When a new revision lands:

1. Save the latest mock as `v<N>-final.png` here (use the latest stable revision; skip intermediate scrolls).
2. Keep prior versions — don't overwrite. Each revision is a snapshot in time.
3. Update this README's table with a one-line summary of what changed and why.
4. If the change is meaningful (new pane, dropped element, architectural shift), update [`../design-frontend-plan.md`](../design-frontend-plan.md) accordingly and bump the relevant Linear ticket descriptions.
5. Optionally append a short ADR to `../decisions/` if the design change reflects a principled decision (e.g. dropping a feature, changing data source).

## Why we store these in-repo

- **AC verification at PR time** needs a stable reference; Claude Design URLs aren't guaranteed permanent.
- **Future engineers** picking up Phase 6 tickets can see what was scoped without needing to bug the user for links.
- **Design history** matters when answering "why isn't there an FWD column?" or "why is sentiment three classes?" — the v1→v2 diff captures the rationale.
