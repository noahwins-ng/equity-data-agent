"""Quick dev seed: materialize the ingestion assets for a few tickers.

``make seed`` runs ``python -m dagster_pipelines.seed`` for a fast local dataset
without opening the Dagster UI. It drives the three source-ingest assets
directly (the same way the test suite invokes them) against whatever ClickHouse
``shared.Settings`` points at -- in dev that is prod ClickHouse over the SSH
tunnel (``make tunnel``), so run the tunnel first.

Scope: 30 days / 3 tickers of raw ingestion (OHLCV + fundamentals + news), the
"fast dev data" the Makefile advertises. Derived/indicator assets are out of
scope -- materialize those from the Dagster UI when needed. Re-runnable:
every ingestion table is a ReplacingMergeTree, so a second seed dedups.
"""

from __future__ import annotations

from dagster import build_asset_context
from shared.tickers import TICKERS

from dagster_pipelines.assets.fundamentals import fundamentals
from dagster_pipelines.assets.news_raw import NewsRawConfig, news_raw
from dagster_pipelines.assets.ohlcv_raw import OHLCVConfig, ohlcv_raw
from dagster_pipelines.resources.clickhouse import ClickHouseResource

# First three portfolio tickers -- enough to exercise every per-ticker path.
SEED_TICKERS = TICKERS[:3]
SEED_OHLCV_PERIOD = "1mo"  # ~30 days
SEED_NEWS_LOOKBACK_DAYS = 30


def main() -> None:
    clickhouse = ClickHouseResource()
    for ticker in SEED_TICKERS:
        print(f"seeding {ticker} ...")
        ohlcv_raw(
            build_asset_context(partition_key=ticker),
            OHLCVConfig(period=SEED_OHLCV_PERIOD),
            clickhouse=clickhouse,
        )
        fundamentals(build_asset_context(partition_key=ticker), clickhouse=clickhouse)
        news_raw(
            build_asset_context(partition_key=ticker),
            NewsRawConfig(lookback_days=SEED_NEWS_LOOKBACK_DAYS),
            clickhouse=clickhouse,
        )
    print(f"seed complete: {', '.join(SEED_TICKERS)}")


if __name__ == "__main__":
    main()
