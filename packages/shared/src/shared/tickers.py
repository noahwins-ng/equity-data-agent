TICKERS: list[str] = [
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN",
    "META", "TSLA", "JPM", "V", "UNH",
]

TICKER_METADATA: dict[str, dict[str, str]] = {
    "NVDA": {"sector": "Technology",             "industry": "Semiconductors"},
    "AAPL": {"sector": "Technology",             "industry": "Consumer Electronics"},
    "MSFT": {"sector": "Technology",             "industry": "Software"},
    "GOOGL": {"sector": "Technology",            "industry": "Internet Content & Information"},
    "AMZN": {"sector": "Consumer Discretionary", "industry": "Internet & Direct Marketing Retail"},
    "META": {"sector": "Technology",             "industry": "Internet Content & Information"},
    "TSLA": {"sector": "Consumer Discretionary", "industry": "Automobiles"},
    "JPM":  {"sector": "Financials",             "industry": "Diversified Banks"},
    "V":    {"sector": "Financials",             "industry": "Transaction & Payment Processing"},
    "UNH":  {"sector": "Healthcare",             "industry": "Managed Health Care"},
}
