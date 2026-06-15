CREATE TABLE IF NOT EXISTS schema_migrations (
    filename String,
    applied_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(applied_at)
ORDER BY filename;

ALTER TABLE equity_raw.ohlcv_raw MODIFY COMMENT 'Raw daily OHLCV bars from yfinance; split-adjusted price is adj_close and raw close is close.';
ALTER TABLE equity_raw.ohlcv_raw COMMENT COLUMN ticker 'Portfolio ticker symbol from shared.tickers.TICKERS.';
ALTER TABLE equity_raw.ohlcv_raw COMMENT COLUMN date 'Trading date for the daily bar.';
ALTER TABLE equity_raw.ohlcv_raw COMMENT COLUMN open 'Unadjusted opening price in USD.';
ALTER TABLE equity_raw.ohlcv_raw COMMENT COLUMN high 'Unadjusted intraday high price in USD.';
ALTER TABLE equity_raw.ohlcv_raw COMMENT COLUMN low 'Unadjusted intraday low price in USD.';
ALTER TABLE equity_raw.ohlcv_raw COMMENT COLUMN close 'Unadjusted closing price in USD.';
ALTER TABLE equity_raw.ohlcv_raw COMMENT COLUMN adj_close 'Split/dividend-adjusted closing price in USD; chart source of truth.';
ALTER TABLE equity_raw.ohlcv_raw COMMENT COLUMN volume 'Daily traded share volume.';
ALTER TABLE equity_raw.ohlcv_raw COMMENT COLUMN fetched_at 'Ingestion timestamp used as ReplacingMergeTree version.';

ALTER TABLE equity_raw.fundamentals MODIFY COMMENT 'Raw quarterly and annual fundamental statement snapshots used to derive ratios.';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN ticker 'Portfolio ticker symbol from shared.tickers.TICKERS.';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN period_end 'Fiscal period end date.';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN period_type 'Fiscal period type; expected values are quarterly and annual.';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN revenue 'Revenue for the reporting period in USD.';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN gross_profit 'Gross profit for the reporting period in USD.';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN net_income 'Net income for the reporting period in USD.';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN free_cash_flow 'Free cash flow for the reporting period in USD.';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN ebitda 'EBITDA for the reporting period in USD.';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN shares_outstanding 'Shares outstanding from the source statement snapshot.';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN implied_shares_outstanding 'Shares inferred from market cap divided by price when available; defaults to shares_outstanding.';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN market_cap 'Market capitalization snapshot in USD.';
ALTER TABLE equity_raw.fundamentals COMMENT COLUMN fetched_at 'Ingestion timestamp used as ReplacingMergeTree version.';

ALTER TABLE equity_raw.news_raw MODIFY COMMENT 'Irreplaceable raw Finnhub company news articles; Qdrant embeddings regenerate from this table.';
ALTER TABLE equity_raw.news_raw COMMENT COLUMN id 'Stable UInt64 article id derived from the article URL.';
ALTER TABLE equity_raw.news_raw COMMENT COLUMN ticker 'Portfolio ticker symbol; cross-mentioned stories are stored once per ticker.';
ALTER TABLE equity_raw.news_raw COMMENT COLUMN headline 'Article headline from Finnhub.';
ALTER TABLE equity_raw.news_raw COMMENT COLUMN body 'Article summary/body text from Finnhub.';
ALTER TABLE equity_raw.news_raw COMMENT COLUMN source 'Source label supplied by Finnhub.';
ALTER TABLE equity_raw.news_raw COMMENT COLUMN url 'Canonical article URL used for id generation.';
ALTER TABLE equity_raw.news_raw COMMENT COLUMN published_at 'Article publication timestamp.';
ALTER TABLE equity_raw.news_raw COMMENT COLUMN publisher_name 'Canonical publisher name for frontend display.';
ALTER TABLE equity_raw.news_raw COMMENT COLUMN image_url 'Optional article image URL for frontend news cards.';
ALTER TABLE equity_raw.news_raw COMMENT COLUMN sentiment_label 'Sentiment classifier status/label; pending means not classified.';
ALTER TABLE equity_raw.news_raw COMMENT COLUMN resolved_host 'Resolved publisher host after following Finnhub redirects.';
ALTER TABLE equity_raw.news_raw COMMENT COLUMN fetched_at 'Ingestion timestamp used as ReplacingMergeTree version.';

ALTER TABLE equity_derived.technical_indicators_daily MODIFY COMMENT 'Daily technical indicators computed from daily OHLCV bars; nullable values mark warm-up windows.';
ALTER TABLE equity_derived.technical_indicators_weekly MODIFY COMMENT 'Weekly technical indicators computed from derived weekly OHLCV bars; nullable values mark warm-up windows.';
ALTER TABLE equity_derived.technical_indicators_monthly MODIFY COMMENT 'Monthly technical indicators computed from derived monthly OHLCV bars; nullable values mark warm-up windows.';

ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN ticker 'Portfolio ticker symbol from shared.tickers.TICKERS.';
ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN date 'Indicator date matching the source OHLCV period.';
ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN rsi_14 '14-period RSI oscillator; expected range is 0 to 100.';
ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN macd 'MACD line using 12/26-period EMAs.';
ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN macd_signal 'MACD signal line using 9-period EMA.';
ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN macd_hist 'MACD histogram: macd minus macd_signal.';
ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN sma_20 '20-period simple moving average of adjusted close in USD.';
ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN sma_50 '50-period simple moving average of adjusted close in USD.';
ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN sma_200 '200-period simple moving average of adjusted close in USD.';
ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN adx_14 '14-period average directional index trend-strength indicator.';
ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN atr_14 '14-period average true range in price units.';
ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN obv 'On-balance volume cumulative indicator.';
ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN bb_pct_b 'Bollinger Band percent-B value.';
ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN macd_bullish_cross '1 when a bullish MACD crossover is detected for the period, otherwise 0.';
ALTER TABLE equity_derived.technical_indicators_daily COMMENT COLUMN computed_at 'Computation timestamp used as ReplacingMergeTree version.';

ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN ticker 'Portfolio ticker symbol from shared.tickers.TICKERS.';
ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN week_start 'Week start date matching the source weekly OHLCV period.';
ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN rsi_14 '14-period RSI oscillator; expected range is 0 to 100.';
ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN macd 'MACD line using 12/26-period EMAs.';
ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN macd_signal 'MACD signal line using 9-period EMA.';
ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN macd_hist 'MACD histogram: macd minus macd_signal.';
ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN sma_20 '20-period simple moving average of adjusted close in USD.';
ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN sma_50 '50-period simple moving average of adjusted close in USD.';
ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN sma_200 '200-period simple moving average of adjusted close in USD.';
ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN adx_14 '14-period average directional index trend-strength indicator.';
ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN atr_14 '14-period average true range in price units.';
ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN obv 'On-balance volume cumulative indicator.';
ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN bb_pct_b 'Bollinger Band percent-B value.';
ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN macd_bullish_cross '1 when a bullish MACD crossover is detected for the period, otherwise 0.';
ALTER TABLE equity_derived.technical_indicators_weekly COMMENT COLUMN computed_at 'Computation timestamp used as ReplacingMergeTree version.';

ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN ticker 'Portfolio ticker symbol from shared.tickers.TICKERS.';
ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN month_start 'Month start date matching the source monthly OHLCV period.';
ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN rsi_14 '14-period RSI oscillator; expected range is 0 to 100.';
ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN macd 'MACD line using 12/26-period EMAs.';
ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN macd_signal 'MACD signal line using 9-period EMA.';
ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN macd_hist 'MACD histogram: macd minus macd_signal.';
ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN sma_20 '20-period simple moving average of adjusted close in USD.';
ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN sma_50 '50-period simple moving average of adjusted close in USD.';
ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN sma_200 '200-period simple moving average of adjusted close in USD.';
ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN adx_14 '14-period average directional index trend-strength indicator.';
ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN atr_14 '14-period average true range in price units.';
ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN obv 'On-balance volume cumulative indicator.';
ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN bb_pct_b 'Bollinger Band percent-B value.';
ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN macd_bullish_cross '1 when a bullish MACD crossover is detected for the period, otherwise 0.';
ALTER TABLE equity_derived.technical_indicators_monthly COMMENT COLUMN computed_at 'Computation timestamp used as ReplacingMergeTree version.';

ALTER TABLE equity_derived.fundamental_summary MODIFY COMMENT 'Derived valuation, growth, profitability, cash-flow, leverage, and liquidity ratios.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN ticker 'Portfolio ticker symbol from shared.tickers.TICKERS.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN period_end 'Fiscal period end date.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN period_type 'Fiscal period type; expected values are quarterly and annual.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN pe_ratio 'Price-to-earnings ratio; quarterly values use trailing-four-quarter net income.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN ev_ebitda 'Enterprise value to EBITDA ratio.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN price_to_book 'Price-to-book ratio.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN price_to_sales 'Price-to-sales ratio.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN eps 'Earnings per share in USD.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN revenue_yoy_pct 'Year-over-year revenue growth percentage.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN net_income_yoy_pct 'Year-over-year net income growth percentage.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN fcf_yoy_pct 'Year-over-year free cash flow growth percentage.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN net_margin_pct 'Net income divided by revenue, expressed as a percentage.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN gross_margin_pct 'Gross profit divided by revenue, expressed as a percentage.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN ebitda_margin_pct 'EBITDA divided by revenue, expressed as a percentage.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN gross_margin_bps_yoy 'Year-over-year gross margin change in basis points.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN net_margin_bps_yoy 'Year-over-year net margin change in basis points.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN revenue_ttm 'Trailing-twelve-month revenue in USD.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN net_income_ttm 'Trailing-twelve-month net income in USD.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN fcf_ttm 'Trailing-twelve-month free cash flow in USD.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN roe 'Return on equity ratio.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN roa 'Return on assets ratio.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN fcf_yield 'Free cash flow yield ratio.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN debt_to_equity 'Total debt divided by equity.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN current_ratio 'Current assets divided by current liabilities.';
ALTER TABLE equity_derived.fundamental_summary COMMENT COLUMN computed_at 'Computation timestamp used as ReplacingMergeTree version.';
