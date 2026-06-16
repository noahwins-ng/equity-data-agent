CREATE TABLE IF NOT EXISTS equity_raw.ingest_rejects (
    rejected_at  DateTime DEFAULT now(),
    ticker       LowCardinality(String),
    source_asset LowCardinality(String),
    reason       LowCardinality(String),
    detail       String,
    raw_payload  String,
    id           UInt64
) ENGINE = ReplacingMergeTree(rejected_at)
PARTITION BY ticker
ORDER BY (ticker, source_asset, reason, id)
TTL rejected_at + INTERVAL 90 DAY DELETE;
