# Portfolio screenshots

This directory holds the three screenshots embedded in the repo `README.md` (above-the-fold "Screenshots" section). The README references them by filename; missing files render as broken images on GitHub, so capture all three before the next deploy.

Each screenshot has a single canonical filename and a single canonical capture command — pick a representative ticker (NVDA is the default for the bench) and follow the recipe.

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

## 2. `dagster-lineage.png`

The `ohlcv_raw → ohlcv_weekly / technical_indicators / fundamental_summary` lineage graph with up-to-date materialization decorations.

```bash
make dev-dagster   # http://localhost:3000
```

Navigate: top nav → **Assets** → **Asset graph** → toggle the `equity_pipelines` location on. Frame the screenshot so you can see (a) the `ohlcv_raw` source partitioned-by-ticker, (b) the `weekly` / `monthly` aggregation fan-out, (c) `technical_indicators_daily/weekly/monthly`, and (d) `fundamental_summary`. Save as `docs/screenshots/dagster-lineage.png`.

Resolution target: 1600 px wide minimum. Crop tightly — the graph is wide.

## 3. `cli-thesis.png`

A representative CLI thesis showing the structured Setup / Bull Case / Bear Case / Verdict layout that landed in [QNT-133](https://linear.app/noahwins/issue/QNT-133).

```bash
uv run python -m agent analyze NVDA | tee docs/screenshots/cli-thesis.txt
```

Then take a high-DPI terminal screenshot of the rendered output (most native macOS terminals: ⌘⇧4 then space then click the terminal window). Save as `docs/screenshots/cli-thesis.png`.

Pick a ticker whose thesis includes both bull and bear bullets so the asymmetry-handling design is visible — NVDA, AAPL, and V have all produced balanced theses on recent runs; TSLA tends to produce 0-bull / 3-bear theses (also a fine demo of the "forced symmetry" guard, see QNT-133's ticket body).

The committed `.txt` mirror lets you regenerate the screenshot later with consistent formatting.

## Re-capture cadence

Re-take all three when:

- The agent prompt or thesis structure changes materially (e.g., a new section in `Thesis` schema). The CLI thesis screenshot anchors the design v2 contract — stale screenshots make the README lie about the product.
- The Dagster asset graph changes shape (new asset, dropped asset, restructured deps). The lineage screenshot has to match `make build`.
- The Langfuse trace structure changes (e.g., extra graph nodes, renamed spans).

Routine re-runs of the same content (different ticker, different day) are NOT a reason to re-capture — the screenshots show *how* the system reports, not *what's true today*.
