TICKERS: list[str] = [
    "NVDA",
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "META",
    "TSLA",
    "MU",
    "AMD",
    "INTC",
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
    "MU": {
        "name": "Micron Technology",
        "sector": "Technology",
        "industry": "Semiconductors",
        "description": (
            "Makes memory and storage: DRAM (the majority of revenue) and NAND "
            "flash, sold into data center, mobile, PC, auto, and industrial "
            "markets. High-bandwidth memory (HBM) for AI accelerators is the "
            "current growth driver. Commodity-cyclical with boom/bust pricing."
        ),
        "key_competitors": ["Samsung", "SK Hynix", "Kioxia", "Western Digital", "SanDisk"],
        "key_risks": [
            "DRAM/NAND price cyclicality (oversupply downcycles)",
            "Capital intensity of leading-edge fabs",
            "HBM execution vs. SK Hynix and Samsung",
            "China export controls and demand exposure",
        ],
        "watch": [
            "DRAM and NAND pricing trend",
            "HBM revenue ramp",
            "Gross margin",
            "Inventory and capex",
        ],
    },
    "AMD": {
        "name": "Advanced Micro Devices",
        "sector": "Technology",
        "industry": "Semiconductors",
        "description": (
            "Designs CPUs (Ryzen client, EPYC server), GPUs (Radeon gaming, "
            "Instinct data-center AI accelerators), and adaptive/embedded silicon "
            "(Xilinx). Data Center — EPYC share gains plus the Instinct MI "
            "accelerator line — is the growth engine against NVIDIA and Intel."
        ),
        "key_competitors": ["NVIDIA", "Intel", "Broadcom", "Qualcomm", "ARM"],
        "key_risks": [
            "NVIDIA dominance in AI GPUs",
            "Foundry dependence on TSMC",
            "PC and gaming demand cyclicality",
            "Execution on the Instinct accelerator roadmap",
        ],
        "watch": [
            "Data Center GPU (Instinct MI) revenue",
            "Server CPU share gains vs. Intel",
            "Gross margin",
            "AI accelerator guidance",
        ],
    },
    "INTC": {
        "name": "Intel",
        "sector": "Technology",
        "industry": "Semiconductors",
        "description": (
            "Integrated device manufacturer: designs and fabricates x86 CPUs for "
            "client (PC) and data center, and is standing up Intel Foundry to "
            "build chips for external customers. Mid-turnaround — racing to "
            "regain process leadership (18A) while defending CPU share."
        ),
        "key_competitors": ["AMD", "NVIDIA", "TSMC", "Samsung Foundry", "ARM"],
        "key_risks": [
            "Process-node execution risk (18A yield and ramp)",
            "Foundry build-out losses and capex strain",
            "Server/PC share loss to AMD and ARM",
            "Missing the AI accelerator wave",
        ],
        "watch": [
            "18A yield and ramp",
            "Intel Foundry external wins and operating loss",
            "Data Center & AI revenue",
            "Gross margin and capex",
        ],
    },
    "SPY": {"name": "S&P 500 ETF", "sector": "Benchmark", "industry": "S&P 500 ETF"},
}

# Keys every portfolio ticker's TICKER_METADATA entry must carry for the
# company-knowledge tool. SPY/benchmark is exempt — it never reaches that
# endpoint (gated on TICKERS) and keeps the original three-key form.
_REQUIRED_METADATA_KEYS = frozenset(
    {"name", "sector", "industry", "description", "key_competitors", "key_risks", "watch"}
)


def _validate_metadata_coverage(
    tickers: list[str], metadata: dict[str, dict[str, str | list[str]]]
) -> None:
    """Raise AssertionError if any ticker lacks a full metadata entry.

    Symmetric to the NEWS_RELEVANCE assert below: a ticker added to TICKERS
    without a complete TICKER_METADATA entry should fail at import, not at
    request time inside the company-knowledge tool.
    """
    for ticker in tickers:
        missing = _REQUIRED_METADATA_KEYS - metadata.get(ticker, {}).keys()
        assert not missing, (
            f"TICKER_METADATA[{ticker!r}] missing required keys: {sorted(missing)}. "
            "Every portfolio ticker needs a full editorial profile so the "
            "company-knowledge tool resolves at import, not at request time."
        )


_validate_metadata_coverage(TICKERS, TICKER_METADATA)

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
#                       lowercase "meta" appears in metadata/metaphor; INTC's
#                       "Intel" collides with lowercase "intel" (intelligence).
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
    "MU": {"aliases": ["MU", "Micron", "Micron Technology"], "scope": "any"},
    "AMD": {"aliases": ["AMD", "Advanced Micro Devices", "Lisa Su"], "scope": "any"},
    # INTC uses scope="headline": bare "Intel" collides with lowercase "intel"
    # (intelligence) in body prose — the same high-false-positive class as
    # META's "meta". Headline scope keeps the company match while dropping the
    # body-prose noise. The bare symbol "INTC" stays in aliases (unlike META/V,
    # which drop theirs) since "INTC" has no prose collision.
    "INTC": {"aliases": ["INTC", "Intel", "Intel Corporation"], "scope": "headline"},
}

assert set(NEWS_RELEVANCE.keys()) == set(TICKERS), (
    "NEWS_RELEVANCE must cover every TICKERS entry. Adding a ticker requires "
    "a relevance config entry here so news ingest can keep/drop its articles."
)

# QNT-257: company-name -> ticker resolution for the agent. The chat ticker
# parser (agent.intent.extract_tickers) historically matched only the literal
# symbol, so "thesis on micron" resolved to nothing and bounced to the clarify
# node, while "thesis on MU" worked. This map lets the parser recognise the
# plain company name a user actually types.
#
# Deliberately CONSERVATIVE and distinct from NEWS_RELEVANCE.aliases: the news
# aliases are tuned for article keep/drop (they include exec names like "Musk"
# and product brands like "AWS"/"Azure"/"CUDA"), which over-resolve in chat
# prose. Here we list only the company name + the common short name a user would
# type. No exec names, no product brands (out of scope per QNT-257). The bare
# symbol is matched separately by the parser, so it is NOT repeated here.
#
# Collision notes (accepted trade-offs, pinned in tests/agent/test_intent.py):
#   * "Meta"/"Facebook" -> META. Matched on word boundary, so "metadata" /
#     "metaphor" do NOT resolve; a standalone "meta" token does (rare in an
#     equities chat).
#   * "Intel" -> INTC. Lowercase "intel" meaning "intelligence" is a theoretical
#     collision, but a chat ask naming "intel" is overwhelmingly the company.
#   * Common-word names ("apple", "amazon", "google", "tesla", "micron") are
#     themselves English/unit words, so "tesla coil", "apple juice", "amazon
#     rainforest", "sub-micron" resolve to the ticker. This is the deliberate
#     cost of plain-name resolution at the conservative cut: you cannot catch
#     "what's tesla's thesis" without also catching "tesla coil" unless you add
#     context-aware NER (out of scope). The boundary is alpha-only on purpose --
#     tightening it to block hyphens would also drop legitimate "Micron-based" /
#     "Tesla-built" mentions. These are pinned as accepted in
#     test_extract_tickers_accepted_common_word_collisions; revisit with an
#     entity-disambiguation pass if any becomes loud in production.
#
# Adding a ticker requires an entry here; the assert below enforces coverage so
# the registry and this map can never drift (mirrors NEWS_RELEVANCE / metadata).
TICKER_NAME_ALIASES: dict[str, list[str]] = {
    "NVDA": ["Nvidia"],
    "AAPL": ["Apple"],
    "MSFT": ["Microsoft"],
    "GOOGL": ["Google", "Alphabet"],
    "AMZN": ["Amazon"],
    "META": ["Meta", "Facebook"],
    "TSLA": ["Tesla"],
    "MU": ["Micron"],
    "AMD": ["Advanced Micro Devices"],
    "INTC": ["Intel"],
}

assert set(TICKER_NAME_ALIASES.keys()) == set(TICKERS), (
    "TICKER_NAME_ALIASES must cover every TICKERS entry. Adding a ticker "
    "requires a company-name alias entry here so the agent's chat parser can "
    "resolve the plain company name, not just the symbol."
)
