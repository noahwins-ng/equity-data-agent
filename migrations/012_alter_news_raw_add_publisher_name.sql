ALTER TABLE equity_raw.news_raw
ADD COLUMN IF NOT EXISTS publisher_name LowCardinality(String) DEFAULT '';
