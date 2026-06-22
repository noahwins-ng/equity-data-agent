# Portfolio screenshots

This directory holds the four screenshots embedded in the repo `README.md` (the hero shot plus the "Screenshots" section). The README references them by filename; missing files render as broken images on GitHub, so capture all four before the next deploy.

Each screenshot has a single canonical filename and a single canonical capture command — pick a representative ticker (NVDA is the default for the bench) and follow the recipe.

> **Refresh status (2026-06-22):** `terminal-live.png` and `dagster-lineage.svg` are stale — see the per-item notes below. `langfuse-trace.png` and `cli-thesis.png` are current. Two more (`rag-provenance.png`, `dagster-asset-checks.png`) are planned but not yet captured — see §5–§6.

## 1. `langfuse-trace.png`

A full `plan → gather → synthesize` agent run with tool-call latencies and per-step token usage.

```bash
make tunnel        # terminal 1 (ClickHouse)
make dev-litellm   # terminal 2
make dev-api       # terminal 3
uv run python -m agent analyze NVDA
```

Open the run in Langfuse: https://us.cloud.langfuse.com → your project → Traces → click the most recent trace → expand the `synthesize` span → screenshot the full timeline (including tool-call latencies and token counts). Save as `docs/screenshots/langfuse-trace.png`.

Resolution target: 1600 px wide minimum so the timeline is readable on desktop GitHub.

## 2. `dagster-lineage.svg`

The `ohlcv_raw → ohlcv_weekly / technical_indicators / fundamental_summary` lineage graph with up-to-date materialization decorations.

**⚠ Needs refresh (captured 2026-04-27).** Missing the `earnings_releases_raw` + `earnings_embeddings` assets added since — the graph shows 10 assets, but there are 12 now. Re-export so it matches the current asset list.

```bash
make dev-dagster   # http://localhost:3000
```

Navigate: top nav → **Assets** → **Asset graph** → toggle the `equity_pipelines` location on. Frame the view so you can see (a) the `ohlcv_raw` source partitioned-by-ticker, (b) the `weekly` / `monthly` aggregation fan-out, (c) `technical_indicators_daily/weekly/monthly`, (d) `fundamental_summary`, and (e) the `news_raw → news_embeddings` and `earnings_releases_raw → earnings_embeddings` branches. Export as SVG (Dagster UI: top-right of the asset graph) and save as `docs/screenshots/dagster-lineage.svg`.

SVG keeps the graph crisp at any width and renders inline on GitHub. If you fall back to PNG, target 1600 px wide minimum and crop tightly — the graph is wide.

## 3. `cli-thesis.png`

A representative CLI thesis showing the structured Setup / Bull Case / Bear Case / Verdict layout that landed in [QNT-133](https://linear.app/noahwins/issue/QNT-133).

```bash
uv run python -m agent analyze NVDA | tee docs/screenshots/cli-thesis.txt
```

Then take a high-DPI terminal screenshot of the rendered output (most native macOS terminals: ⌘⇧4 then space then click the terminal window). Save as `docs/screenshots/cli-thesis.png`.

Pick a ticker whose thesis includes both bull and bear bullets so the asymmetry-handling design is visible — NVDA, AAPL, and V have all produced balanced theses on recent runs; TSLA tends to produce 0-bull / 3-bear theses (also a fine demo of the "forced symmetry" guard, see QNT-133's ticket body).

The committed `.txt` mirror lets you regenerate the screenshot later with consistent formatting.

## 4. `terminal-live.png`

The hero shot (top of the README, reused in the "Screenshots" section): the three-pane terminal — watchlist (left), ticker detail (center: quote header, price chart, technicals / fundamentals / news cards), and the persistent chat panel (right).

**⚠ Needs refresh (captured 2026-05-09).** Predates the mid-June ticker swap, so the watchlist still shows V/JPM/UNH instead of MU/AMD/INTC (contradicts the universe listed in the README), and it predates the RAG retrieved-sources + earnings chat cards. Capture from the live site rather than local dev for a fully populated view:

1. Open the live app: https://equity-data-agent-ynr2.vercel.app
2. Select a ticker with a full detail view (AAPL or NVDA) so the quote header, chart, and all three cards render.
3. Optionally run a chat prompt that triggers RAG (e.g. `What's the latest on TSLA litigation?`) so the retrieved-sources provenance is visible in the chat panel.
4. Screenshot the full browser viewport at 1600 px wide minimum and save as `docs/screenshots/terminal-live.png`.

Frame it so all three panes are visible — the persistent three-pane workspace is the point.

## 5. `rag-provenance.png` (planned — not yet captured)

The chat panel rendering retrieved-sources provenance (citation links/chips under an answer) — backs the grounded-RAG claim in the README's AI Engineering section, which currently has no image.

Capture from the live site:

1. Open the live app: https://equity-data-agent-ynr2.vercel.app
2. Ask a targeted-event prompt that fires RAG, e.g. `What's the latest on TSLA litigation?` or `Any buyback news on AAPL?`
3. Once the answer streams with the retrieved-sources list, screenshot the chat panel (1200 px wide minimum) and save as `docs/screenshots/rag-provenance.png`.

Not yet referenced in the repo `README.md` — add the `![...]` embed (AI Engineering or Screenshots section) once captured.

## 6. `dagster-asset-checks.png` (planned — not yet captured)

The Dagster asset-checks view showing passed/failed domain-bounded checks (RSI 0-100, P/E band, MACD coherence, etc.) — backs the "37 domain-bounded asset checks (dbt-test equivalent)" claim, the strongest DE differentiator with no image today.

```bash
make dev-dagster   # http://localhost:3000 (tunnel up), or SSH-tunnel to prod Dagster
```

Navigate: top nav → **Assets** → pick an asset with checks (e.g. `fundamental_summary` or `technical_indicators_daily`) → the **Checks** tab (or the global asset-checks list). Frame several checks with pass/fail status and a bound description visible. Screenshot 1600 px wide minimum and save as `docs/screenshots/dagster-asset-checks.png`.

Not yet referenced in the repo `README.md` — add the `![...]` embed once captured.

## Re-capture cadence

Re-take all three when:

- The agent prompt or thesis structure changes materially (e.g., a new section in `Thesis` schema). The CLI thesis screenshot anchors the design v2 contract — stale screenshots make the README lie about the product.
- The Dagster asset graph changes shape (new asset, dropped asset, restructured deps). The lineage screenshot has to match `make build`.
- The Langfuse trace structure changes (e.g., extra graph nodes, renamed spans).

Routine re-runs of the same content (different ticker, different day) are NOT a reason to re-capture — the screenshots show *how* the system reports, not *what's true today*.
