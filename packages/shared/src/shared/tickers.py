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

# Per-ticker keep/drop gate for news_raw ingest. Finnhub /company-news returns
# articles tagged with the ticker in their `related` field, which includes
# sector roundups and peer-comparison pieces where the ticker is a one-line
# peripheral mention. Without a relevance gate every such article lands under
# the ticker's news feed.
#
# An article is kept iff at least one alias matches case-insensitive,
# word-boundary anywhere in the configured scope.
#
#   scope="any"      -> match in headline OR body
#   scope="headline" -> match in headline only; used when the symbol/name has
#                       a high false-positive rate inside body prose. META's
#                       lowercase "meta" appears in metadata/metaphor; "V"
#                       alone matches arbitrary words.
#
# Adding a ticker requires adding an entry here; the assert at module load
# enforces this so the registry and the relevance config can never drift.
#
# Known limitations (acceptable trade-offs for a regex gate, revisit if any
# becomes loud in production):
#   * The strict boundary in news_raw._RELEVANCE_PATTERNS treats hyphens as
#     word characters, so "NVIDIA-powered GPUs", "Apple-designed silicon",
#     "Tesla-built batteries" all DROP. Add the hyphenated form as its own
#     alias if a particular pattern becomes a recurring miss.
#   * "Amazon" under AMZN scope=any keeps Amazon-River / Amazon-rainforest
#     macro and ESG coverage that mentions the company in passing. Rare on
#     a finance feed; not worth the brand-narrowing cost.
#   * "Musk" under TSLA scope=any keeps SpaceX, xAI, X/Twitter, and Boring
#     Company stories. Intentional: TSLA-investor news cycle is dominated by
#     Musk's other ventures and most coverage that matters mentions him.
NEWS_RELEVANCE: dict[str, dict[str, object]] = {
    "NVDA": {"aliases": ["NVDA", "Nvidia", "GeForce", "CUDA"], "scope": "any"},
    "AAPL": {"aliases": ["AAPL", "Apple", "iPhone", "Tim Cook"], "scope": "any"},
    "MSFT": {"aliases": ["MSFT", "Microsoft", "Azure", "Satya Nadella"], "scope": "any"},
    "GOOGL": {"aliases": ["GOOGL", "GOOG", "Alphabet", "Google"], "scope": "any"},
    "AMZN": {"aliases": ["AMZN", "Amazon", "AWS", "Andy Jassy"], "scope": "any"},
    "META": {
        "aliases": ["META", "Meta Platforms", "Facebook", "Instagram", "WhatsApp", "Zuckerberg"],
        "scope": "headline",
    },
    "TSLA": {"aliases": ["TSLA", "Tesla", "Musk"], "scope": "any"},
    "JPM": {"aliases": ["JPM", "JPMorgan", "JP Morgan", "Jamie Dimon"], "scope": "any"},
    "V": {"aliases": ["Visa Inc", "Visa"], "scope": "headline"},
    "UNH": {"aliases": ["UNH", "UnitedHealth", "UnitedHealthcare"], "scope": "any"},
}

assert set(NEWS_RELEVANCE.keys()) == set(TICKERS), (
    "NEWS_RELEVANCE must cover every TICKERS entry. Adding a ticker requires "
    "a relevance config entry here so news ingest can keep/drop its articles."
)
