ALTER TABLE equity_raw.news_raw
ADD COLUMN IF NOT EXISTS image_url String DEFAULT '';
