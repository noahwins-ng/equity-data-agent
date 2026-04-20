"""RSS feed URL definitions for news ingestion.

Used by QNT-53 `news_raw` asset; verified by `scripts/verify_news_feeds.py`.
Yahoo Finance and MarketWatch RSS are free and unrate-limited at a 10-ticker scope.
"""

from shared.tickers import TICKERS

YAHOO_TICKER_FEED = (
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
)

MARKET_FEEDS: dict[str, str] = {
    "yahoo_markets": "https://finance.yahoo.com/news/rssindex",
    "marketwatch_topstories": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
}


def ticker_feed_url(ticker: str) -> str:
    """Build the Yahoo Finance per-ticker RSS URL."""
    return YAHOO_TICKER_FEED.format(ticker=ticker)


def all_ticker_feeds() -> dict[str, str]:
    """Map every ticker in `shared.tickers.TICKERS` to its RSS URL."""
    return {ticker: ticker_feed_url(ticker) for ticker in TICKERS}
