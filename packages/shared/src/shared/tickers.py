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
    "NVDA": {"sector": "Technology", "industry": "Semiconductors"},
    "AAPL": {"sector": "Technology", "industry": "Consumer Electronics"},
    "MSFT": {"sector": "Technology", "industry": "Software"},
    "GOOGL": {"sector": "Technology", "industry": "Internet Content & Information"},
    "AMZN": {"sector": "Consumer Discretionary", "industry": "Internet & Direct Marketing Retail"},
    "META": {"sector": "Technology", "industry": "Internet Content & Information"},
    "TSLA": {"sector": "Consumer Discretionary", "industry": "Automobiles"},
    "JPM": {"sector": "Financials", "industry": "Diversified Banks"},
    "V": {"sector": "Financials", "industry": "Transaction & Payment Processing"},
    "UNH": {"sector": "Healthcare", "industry": "Managed Health Care"},
    "SPY": {"sector": "Benchmark", "industry": "S&P 500 ETF"},
}
