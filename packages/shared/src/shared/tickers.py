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

# QNT-175: extended each entry with description / key_competitors / key_risks /
# watch so the agent's company-knowledge tool has a static business profile per
# ticker. The four new fields are static editorial — sourced from each company's
# 10-K business overview and risk factors, kept terse so the static report stays
# under a screen and the LLM can quote it verbatim. SPY keeps only the original
# three keys; it never reaches the company-knowledge endpoint (gated on TICKERS).
TICKER_METADATA: dict[str, dict[str, str | list[str]]] = {
    "NVDA": {
        "name": "NVIDIA",
        "sector": "Technology",
        "industry": "Semiconductors",
        "description": (
            "Designs GPUs, data-center accelerators, and the CUDA software stack "
            "that powers most large-scale AI training and inference workloads. "
            "Data Center is the dominant segment; Gaming, Pro Visualization, and "
            "Automotive round out the portfolio."
        ),
        "key_competitors": ["AMD", "Intel", "Broadcom", "Google TPU", "AWS Trainium"],
        "key_risks": [
            "AI capex digestion at hyperscaler customers",
            "China export controls on advanced GPUs",
            "Custom-silicon insourcing by top customers",
            "Cyclical semiconductor demand",
        ],
        "watch": [
            "Data Center revenue growth",
            "Gross margin",
            "Hyperscaler capex commentary",
            "Blackwell / next-gen ramp",
        ],
    },
    "AAPL": {
        "name": "Apple",
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "description": (
            "Designs and sells iPhone, Mac, iPad, Wearables, and a high-margin "
            "Services portfolio (App Store, iCloud, Apple Music, advertising). "
            "iPhone drives roughly half of revenue; Services is the fastest "
            "grower and richest in margin."
        ),
        "key_competitors": ["Samsung", "Google", "Microsoft", "Huawei", "Xiaomi"],
        "key_risks": [
            "iPhone unit-cycle dependence",
            "China demand and supply concentration",
            "App Store regulatory pressure (DOJ, EU DMA)",
            "Slow AI feature rollout vs. peers",
        ],
        "watch": [
            "iPhone revenue YoY",
            "Services revenue growth",
            "Greater China revenue",
            "Gross margin (product vs services mix)",
        ],
    },
    "MSFT": {
        "name": "Microsoft",
        "sector": "Technology",
        "industry": "Software",
        "description": (
            "Three roughly equal segments: Productivity & Business Processes "
            "(Office, LinkedIn), Intelligent Cloud (Azure, server products), "
            "and More Personal Computing (Windows, Xbox, Surface, Search). "
            "Azure and the OpenAI partnership anchor the AI thesis."
        ),
        "key_competitors": ["AWS", "Google Cloud", "Salesforce", "Oracle", "ServiceNow"],
        "key_risks": [
            "Azure growth deceleration",
            "AI capex outpacing monetisation",
            "Activision and competition oversight",
            "Copilot attach and pricing pushback",
        ],
        "watch": [
            "Azure revenue growth (constant currency)",
            "Capex as % of revenue",
            "Operating margin",
            "Microsoft 365 Copilot seats",
        ],
    },
    "GOOGL": {
        "name": "Alphabet",
        "sector": "Technology",
        "industry": "Internet Content & Information",
        "description": (
            "Search and YouTube advertising fund the company; Google Cloud is "
            "the #3 hyperscaler and approaching scale profitability. Other Bets "
            "(Waymo, Verily) is a small loss-making optionality bucket."
        ),
        "key_competitors": ["Meta", "Microsoft (Bing/Azure)", "Amazon", "TikTok", "OpenAI"],
        "key_risks": [
            "Generative-AI substitution for traditional search",
            "DOJ search and ad-tech antitrust remedies",
            "Cloud growth vs. AWS/Azure",
            "YouTube ad pricing and Shorts monetisation",
        ],
        "watch": [
            "Search revenue growth",
            "Google Cloud revenue + operating margin",
            "Capex",
            "Gemini adoption metrics",
        ],
    },
    "AMZN": {
        "name": "Amazon",
        "sector": "Consumer Discretionary",
        "industry": "Internet & Direct Marketing Retail",
        "description": (
            "Three engines: North America and International retail (low margin, "
            "scale), AWS (high margin, cyclical with enterprise IT), and a fast-"
            "growing Advertising business. AWS is the profit centre."
        ),
        "key_competitors": ["Microsoft Azure", "Google Cloud", "Walmart", "Shopify", "Meta (ads)"],
        "key_risks": [
            "AWS growth deceleration vs. Azure",
            "Retail margin compression",
            "FTC antitrust case",
            "Capex on AI infrastructure",
        ],
        "watch": [
            "AWS revenue growth",
            "AWS operating margin",
            "Advertising revenue",
            "North America retail operating margin",
        ],
    },
    "META": {
        "name": "Meta Platforms",
        "sector": "Technology",
        "industry": "Internet Content & Information",
        "description": (
            "Family of Apps (Facebook, Instagram, WhatsApp, Threads) generates "
            "essentially all revenue from advertising. Reality Labs (AR/VR) is "
            "a deliberate multi-year loss centre funding the long-term bet."
        ),
        "key_competitors": ["Google/YouTube", "TikTok", "Snap", "X (Twitter)", "Apple"],
        "key_risks": [
            "Reality Labs losses",
            "AI capex without clear ad-cycle payoff",
            "Apple ATT and platform-policy headwinds",
            "Regulatory pressure (EU DMA, US child-safety bills)",
        ],
        "watch": [
            "Family of Apps revenue growth",
            "Reality Labs operating loss",
            "Capex",
            "Daily active people / engagement",
        ],
    },
    "TSLA": {
        "name": "Tesla",
        "sector": "Consumer Discretionary",
        "industry": "Automobiles",
        "description": (
            "Auto manufacturer (Model 3/Y volume, S/X premium, Cybertruck) plus "
            "Energy Generation & Storage and a long-dated optionality bucket "
            "around Full Self-Driving, robotaxi, and the Optimus humanoid."
        ),
        "key_competitors": ["BYD", "Ford", "GM", "Rivian", "Chinese EV OEMs"],
        "key_risks": [
            "Auto gross margin compression from price cuts",
            "FSD / robotaxi timeline slippage",
            "Demand softness in key EV markets",
            "Key-person and governance concentration around Musk",
        ],
        "watch": [
            "Vehicle deliveries",
            "Auto gross margin (ex-credits)",
            "Energy storage deployments",
            "FSD take rate / regulatory milestones",
        ],
    },
    "JPM": {
        "name": "JPMorgan Chase",
        "sector": "Financials",
        "industry": "Diversified Banks",
        "description": (
            "Largest US bank by assets. Four segments: Consumer & Community "
            "Banking, Corporate & Investment Bank, Commercial Banking, and "
            "Asset & Wealth Management. Diversified earnings stream insulates "
            "against any single-cycle downturn."
        ),
        "key_competitors": [
            "Bank of America",
            "Citigroup",
            "Wells Fargo",
            "Goldman Sachs",
            "Morgan Stanley",
        ],
        "key_risks": [
            "Net interest income sensitivity to rate path",
            "Credit losses if recession hits",
            "Capital rules (Basel III endgame)",
            "Investment-banking fee cyclicality",
        ],
        "watch": [
            "Net interest income",
            "Provision for credit losses",
            "CET1 ratio",
            "Investment banking fees",
        ],
    },
    "V": {
        "name": "Visa",
        "sector": "Financials",
        "industry": "Transaction & Payment Processing",
        "description": (
            "Operates the world's largest card-payments network. Revenue is a "
            "thin take-rate on payments volume, cross-border transactions, and "
            "value-added services — capital-light with structurally high margins."
        ),
        "key_competitors": [
            "Mastercard",
            "American Express",
            "PayPal",
            "Stripe",
            "real-time rails",
        ],
        "key_risks": [
            "Antitrust scrutiny on interchange",
            "Real-time-payments / account-to-account substitution",
            "Cross-border travel volatility",
            "Stablecoin / crypto rails competing for B2B flow",
        ],
        "watch": [
            "Payments volume growth",
            "Cross-border volume",
            "Operating margin",
            "Buyback pace",
        ],
    },
    "UNH": {
        "name": "UnitedHealth",
        "sector": "Healthcare",
        "industry": "Managed Health Care",
        "description": (
            "Two pillars: UnitedHealthcare (the largest US health insurer) and "
            "Optum (health services — OptumRx pharmacy benefits, OptumHealth "
            "care delivery, OptumInsight analytics). Vertical integration is "
            "the structural moat."
        ),
        "key_competitors": ["Elevance Health", "CVS/Aetna", "Cigna", "Humana", "Centene"],
        "key_risks": [
            "Medicare Advantage rate notices and utilisation trend",
            "PBM regulatory scrutiny",
            "Cybersecurity / Change Healthcare aftershocks",
            "Medical loss ratio expansion",
        ],
        "watch": [
            "Medical care ratio (MCR)",
            "UnitedHealthcare membership",
            "Optum revenue growth",
            "EPS guidance",
        ],
    },
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
