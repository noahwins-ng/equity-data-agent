ALTER TABLE equity_raw.news_raw
ADD COLUMN IF NOT EXISTS resolved_host LowCardinality(String) DEFAULT '';
