"""Dev run + verification for the EDGAR earnings-release corpus (QNT-260).

Materialises ``earnings_releases_raw`` then ``earnings_embeddings`` for a set of
tickers against the real ClickHouse + Qdrant resources (exactly the asset code
that runs in prod), then prints the AC5 evidence: per-ticker row counts, a
sample release (ticker / filing_date / section), and the Qdrant point count vs.
the source release count.

Prereqs (from repo root):
    make tunnel          # ClickHouse over SSH (separate terminal)
    make migrate         # applies migration 028 (earnings_releases_raw)
    # QDRANT_URL / QDRANT_API_KEY in .env

Usage:
    uv run --package dagster-pipelines python scripts/verify_earnings_corpus.py
    uv run --package dagster-pipelines python scripts/verify_earnings_corpus.py --tickers NVDA,AAPL

Re-runs are idempotent — ReplacingMergeTree dedups releases on
(ticker, filing_date, doc_id) and Qdrant upserts dedup on the namespaced
point_id, so running this twice is safe.
"""

from __future__ import annotations

import argparse
import logging
import sys

from clickhouse_connect import get_client
from dagster import materialize
from dagster_pipelines.assets.earnings_embeddings import COLLECTION, earnings_embeddings
from dagster_pipelines.assets.earnings_releases_raw import earnings_releases_raw
from dagster_pipelines.resources.clickhouse import ClickHouseResource
from dagster_pipelines.resources.qdrant import QdrantResource
from shared.config import settings
from shared.tickers import TICKERS

logger = logging.getLogger("verify_earnings_corpus")

_TABLE = "equity_raw.earnings_releases_raw"


def _materialize(tickers: list[str]) -> None:
    resources = {"clickhouse": ClickHouseResource(), "qdrant": QdrantResource()}
    for ticker in tickers:
        logger.info("Materialising earnings_releases_raw[%s] ...", ticker)
        materialize([earnings_releases_raw], partition_key=ticker, resources=resources)
        logger.info("Materialising earnings_embeddings[%s] ...", ticker)
        materialize([earnings_embeddings], partition_key=ticker, resources=resources)


def _report(tickers: list[str]) -> None:
    client = get_client(host=settings.CLICKHOUSE_HOST, port=settings.CLICKHOUSE_PORT)
    qdrant = QdrantResource()

    print("\n=== earnings_releases_raw row counts ===")
    counts = client.query_df(
        f"SELECT ticker, count() AS releases, max(filing_date) AS latest "
        f"FROM {_TABLE} FINAL GROUP BY ticker ORDER BY ticker"
    )
    print(counts.to_string(index=False))

    print("\n=== sample release (most recent) ===")
    sample = client.query_df(
        f"SELECT ticker, filing_date, exhibit, left(title, 70) AS title, "
        f"length(body) AS body_chars "
        f"FROM {_TABLE} FINAL ORDER BY filing_date DESC LIMIT 5"
    )
    print(sample.to_string(index=False))

    print(f"\n=== Qdrant {COLLECTION} point count vs source releases ===")
    for ticker in tickers:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        pts = qdrant.count(
            COLLECTION,
            query_filter=Filter(
                must=[FieldCondition(key="ticker", match=MatchValue(value=ticker))]
            ),
        )
        releases = int(
            client.query(
                f"SELECT uniqExact(doc_id) FROM {_TABLE} FINAL WHERE ticker = %(t)s",
                parameters={"t": ticker},
            ).result_rows[0][0]
        )
        print(f"  {ticker}: {pts} Qdrant chunks across {releases} releases")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--tickers", default=",".join(TICKERS))
    parser.add_argument(
        "--report-only", action="store_true", help="Skip materialise; just print counts"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    requested = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    unknown = [t for t in requested if t not in TICKERS]
    if unknown:
        logger.error("Unknown tickers (not in shared.tickers.TICKERS): %s", unknown)
        return 2

    if not args.report_only:
        _materialize(requested)
    _report(requested)
    return 0


if __name__ == "__main__":
    sys.exit(main())
