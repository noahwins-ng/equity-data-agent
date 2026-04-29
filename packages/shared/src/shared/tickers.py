TICKERS: list[str] = [
    "NVDA",
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "META",
    "TSLA",
    "JPM",
    "V",
    "UNH",
]

# Benchmark tickers flow through the OHLCV pipeline only — no fundamentals,
# news, or sentiment. Kept separate so the rest of the system stays at the
# 10-ticker portfolio scope. Endpoints that gate on TICKERS (fundamentals,
# news search, agent reports) reject benchmark symbols by design.
BENCHMARK_TICKERS: list[str] = [
    "SPY",
]

# Convenience union for OHLCV-shaped assets (ohlcv_raw, ohlcv_weekly,
# ohlcv_monthly). Order is preserved so partition listings stay deterministic.
ALL_OHLCV_TICKERS: list[str] = TICKERS + BENCHMARK_TICKERS

TICKER_METADATA: dict[str, dict[str, str]] = {
    "NVDA": {"name": "NVIDIA", "sector": "Technology", "industry": "Semiconductors"},
    "AAPL": {"name": "Apple", "sector": "Technology", "industry": "Consumer Electronics"},
    "MSFT": {"name": "Microsoft", "sector": "Technology", "industry": "Software"},
    "GOOGL": {
        "name": "Alphabet",
        "sector": "Technology",
        "industry": "Internet Content & Information",
    },
    "AMZN": {
        "name": "Amazon",
        "sector": "Consumer Discretionary",
        "industry": "Internet & Direct Marketing Retail",
    },
    "META": {
        "name": "Meta Platforms",
        "sector": "Technology",
        "industry": "Internet Content & Information",
    },
    "TSLA": {"name": "Tesla", "sector": "Consumer Discretionary", "industry": "Automobiles"},
    "JPM": {"name": "JPMorgan Chase", "sector": "Financials", "industry": "Diversified Banks"},
    "V": {"name": "Visa", "sector": "Financials", "industry": "Transaction & Payment Processing"},
    "UNH": {"name": "UnitedHealth", "sector": "Healthcare", "industry": "Managed Health Care"},
    "SPY": {"name": "S&P 500 ETF", "sector": "Benchmark", "industry": "S&P 500 ETF"},
}
