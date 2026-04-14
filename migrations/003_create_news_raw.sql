CREATE TABLE IF NOT EXISTS equity_raw.news_raw (
    id           UInt64,
    ticker       LowCardinality(String),
    headline     String,
    body         String,
    source       String,
    url          String,
    published_at DateTime,
    fetched_at   DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(fetched_at)
PARTITION BY ticker
ORDER BY (ticker, published_at, id);
