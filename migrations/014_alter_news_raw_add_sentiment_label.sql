ALTER TABLE equity_raw.news_raw
ADD COLUMN IF NOT EXISTS sentiment_label LowCardinality(String) DEFAULT 'pending';
