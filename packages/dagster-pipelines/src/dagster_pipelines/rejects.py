"""Reject sink for dropped source rows (QNT-243).

Source-ingest assets drop bad records today and the count vanishes into logs.
This module persists each dropped row to ``equity_raw.ingest_rejects`` and
emits the per-run reject count as Dagster asset metadata, so "what did we
discard, for which ticker, and why" is answerable after the fact (and the
count is available to QNT-240's data-health dashboard).

Scope is SOURCE ingestion only — transform-layer assets operate on
already-validated data. See docs/de-improvement-v1.md Appendix B #1.

Reject rows are reconstructable debug data, so the table carries a 90-day TTL
(migration 027) — it does not grow unbounded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from dagster import AssetExecutionContext, MetadataValue

from dagster_pipelines.resources.clickhouse import ClickHouseResource

REJECTS_TABLE = "equity_raw.ingest_rejects"


@dataclass(frozen=True)
class Reject:
    """One dropped source record.

    ``payload`` is the minimal identifying slice of the original record (not
    necessarily the whole thing) — enough to find it at the source again.
    ``detail`` is free-text context (e.g. the upstream error message).
    """

    ticker: str
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)
    detail: str = ""


def _reject_id(ticker: str, source_asset: str, reason: str, payload: dict[str, Any]) -> int:
    """Deterministic UInt64 dedup key.

    Same dropped record on a re-materialize hashes identically, so the
    ReplacingMergeTree collapses it rather than accumulating duplicates —
    matching the idempotency convention every other equity_raw table follows.
    """
    key = f"{ticker}|{source_asset}|{reason}|{json.dumps(payload, sort_keys=True, default=str)}"
    return int(hashlib.blake2b(key.encode("utf-8"), digest_size=8).hexdigest(), 16)


def _reason_counts(rejects: list[Reject]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rejects:
        counts[r.reason] = counts.get(r.reason, 0) + 1
    return counts


def record_rejects(
    context: AssetExecutionContext,
    clickhouse: ClickHouseResource,
    *,
    source_asset: str,
    rejects: list[Reject],
) -> None:
    """Persist dropped rows to the reject sink and emit a count as asset metadata.

    Always emits ``rejected_rows`` (0 on a clean run) so every materialization
    carries the metric. Only writes to ClickHouse when there is something to
    record.
    """
    context.add_output_metadata(
        {
            "rejected_rows": MetadataValue.int(len(rejects)),
            "reject_reasons": MetadataValue.json(_reason_counts(rejects)),
        }
    )
    if not rejects:
        return

    now = datetime.now(UTC).replace(tzinfo=None)
    rows = [
        {
            "rejected_at": now,
            "ticker": r.ticker,
            "source_asset": source_asset,
            "reason": r.reason,
            "detail": r.detail,
            "raw_payload": json.dumps(r.payload, default=str),
            "id": _reject_id(r.ticker, source_asset, r.reason, r.payload),
        }
        for r in rejects
    ]
    df = pd.DataFrame(rows)
    df["id"] = df["id"].astype("uint64")

    clickhouse.insert_df(REJECTS_TABLE, df)
    context.log.info(
        "Recorded %d reject(s) to %s for %s: %s",
        len(rows),
        REJECTS_TABLE,
        source_asset,
        _reason_counts(rejects),
    )
