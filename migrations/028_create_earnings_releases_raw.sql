CREATE TABLE IF NOT EXISTS equity_raw.earnings_releases_raw (
    doc_id        UInt64,
    ticker        LowCardinality(String),
    cik           String,
    accession     String,
    form          String,
    items         String,
    filing_date   Date,
    period_ending Nullable(Date),
    exhibit       String,
    title         String,
    url           String,
    body          String,
    fetched_at    DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(fetched_at)
PARTITION BY ticker
ORDER BY (ticker, filing_date, doc_id);
