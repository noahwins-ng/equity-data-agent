# Agent answer quality - equity analyst lens

**Date:** 2026-05-18
**Method:** Live-sample assessment. 6 questions covering all 5 answer shapes were posted to `POST /api/v1/agent/chat` and graded against an equity-analyst rubric on three dimensions the existing eval harness does **not** directly score: reasoning structure, actionability, and non-financial-advice disclosure. Numeric grounding is already covered by `evals/hallucination.py` and is not re-scored here.

## What this doc is - and isn't

This is the **forensic snapshot** behind items B-1 through B-7 in [`docs/AI Engineering improvement plan.md`](../AI%20Engineering%20improvement%20plan.md). It captures the verbatim sample outputs, the per-dimension scores, and the reproduction commands so a future re-run produces a directly comparable artifact. **Prescriptive fixes (what to change in the prompts / schema / classifier) live in the roadmap, not here.** When you re-run the assessment in N months, add a sibling file `agent-analyst-quality-<date>.md`; do not edit this one.

## Summary scores (0-3 per dimension; - = not applicable to shape)

| # | Intent | Ticker | Reasoning structure | Actionability | Disclosure |
|---|---|---|---:|---:|---:|
| 1 | thesis | NVDA | 2 | 1 | 0 |
| 2 | thesis | UNH | 2 | 2 | 0 |
| 3 | quick_fact | AAPL | 3 | 2 | 0 |
| 4 | comparison | NVDA vs AAPL | 2 | 1 | 0 |
| 5 | thesis (mis-routed) | TSLA | 3 | 3 | 1 |
| 5b | focused-technical | TSLA | 2 | 1 | 0 |
| 6 | conversational | META | - | - | 1 |

**Roll-up verdict (1-3):**
- Reasoning structure: **2.3** - competent skeletons, but real reasoning errors slip through (RSI-as-bull-while-overbought, "target" used for already-crossed levels). Three of the seven outputs are above 2.
- Actionability: **1.7** - verdict actions name real levels but rarely give a *trigger*. The focused shape is unactionable by prompt design.
- Disclosure: **0.3** - no explicit "not financial advice" language anywhere across all outputs (grep across the repo confirms it doesn't exist in code either). Hedging is the only thing standing between the agent and a buy/sell call.

## Headline findings

1. **Numbers right, framing wrong (sample 1).** NVDA verdict action: `"Close above SMA-50 at 193.07, with a potential target of SMA-20 at 210.30"`. The technical report shows close = **225.32** - *already above* SMA-20 at 210.30 by +7.14%. Calling 210.30 a "potential target" is a directional error: it's a *support level*, not a target. Numbers are real (hallucination scorer would pass), but an analyst would call this out as a logic error. Live evidence the existing scorers can't catch reasoning quality.

2. **Overbought RSI surfaced as a bull point (sample 2).** UNH bull case includes: `"RSI-14: 77.0 - overbought (above 70 threshold; oversold ≤ 30) (source: technical)"`. The report literally prints "overbought" - the LLM quoted the regime label correctly but placed it under "Bull Case." An equity analyst would treat overbought as either neutral context or a mean-reversion *risk*, never a long bullet. The system prompt at `packages/agent/src/agent/prompts/system.py:108-135` tells the model "cite underlying metrics, not the SIGNAL line" - that instruction prevented one failure mode but didn't prevent the model from misclassifying a metric's polarity.

3. **Intent classifier missed a clearly-focused ask (sample 5).** `"Walk me through TSLA technical setup"` routed to `thesis`, not `technical`. The heuristic in `packages/agent/src/agent/intent.py` looks for "technical analysis", "technicals", "ta on", "chart setup" - none of which appear in this phrasing. End user said something a sell-side desk would call a focused-technical request, the LLM fallback didn't catch it either. Ironically, the thesis answer (5) was *better* than the focused-technical re-run (5b) on every dimension because focused intentionally strips the verdict.

4. **Comparison contrasts misread regime semantics (sample 4).** Differences paragraph: `"AAPL shows stronger momentum with a higher RSI-14"` - but AAPL's RSI is 75.9 (overbought), NVDA's is 64.7 (neutral). A higher RSI is not "stronger momentum" when it's past the threshold; it's stretched. The COMPARISON_SYSTEM_PROMPT (`prompts/system.py:315`) asks for qualitative contrast ("trades at a richer multiple", "shows weaker momentum") but the model is treating raw values as ordered scalars rather than regime buckets.

5. **No disclaimer language exists anywhere.** `git grep -i "financial advice\|disclaimer\|informational only\|not investment"` returns zero matches in `packages/agent/` and `packages/api/`. The five system prompts ground the *role* as "investment research analyst" but never qualify that the output is not advice. Verdict actions read as recommendations: `"Close above SMA-50 at 193.07, with a potential target..."` is recommendation-shaped language.

6. **Focused intent is non-actionable by design (sample 5b).** The FOCUSED_SYSTEM_PROMPT (`prompts/system.py:507-516`) explicitly forbids verdict / stance / recommendation: *"Do not produce a four-section thesis. Do not introduce a verdict / stance."* That is a valid product choice but it means a user asking "walk me through TSLA technicals" gets a description with no action - a sell-side equivalent would always end with "watch X" or "trigger at Y."

7. **Focused output violates its own anti-SIGNAL rule (sample 5b).** Summary contains `"The overall signal is bullish, with 3/3 indicators agreeing (source: technical)"`. The system prompt at `prompts/system.py:497-501` says *"Pull underlying metrics or headlines, not the report's own SIGNAL aggregate line."* The model quoted the SIGNAL line anyway. The same anti-SIGNAL guard exists in the thesis prompt but is missing or weaker in the focused prompt body.

## Per-sample verbatim outputs

### Sample 1 - NVDA thesis

> **Setup:** The central question for NVDA is whether the AI capex story is still intact, with the company's Data Center revenue growth and gross margin being key watch metrics (source: company). The technical report indicates a close above SMA-50 and SMA-20, suggesting an uptrend (source: technical), while the fundamental report shows strong growth in revenue and net income (source: fundamental).
>
> **Bull Case:**
> - RSI-14 is neutral at 64.7, indicating a potential for further growth (source: technical)
> - MACD is bullish with a histogram of +1.85, expanding (source: technical)
> - Revenue growth is +73.21% YoY, indicating strong growth (source: fundamental)
> - Nvidia is set to report crucial FQ1'27 earnings with the market forecasting 80% sales growth (source: news)
>
> **Bear Case:**
> - AI capex digestion at hyperscaler customers is a key risk (source: company)
> - China export controls on advanced GPUs are a risk (source: company)
> - Custom-silicon insourcing by top customers is a risk (source: company)
> - Tencent Holdings is facing halted deliveries of advanced Nvidia AI chips (source: news)
>
> **Verdict:** constructive - Close above SMA-50 at 193.07, with a potential target of SMA-20 at 210.30 (source: technical)

- Reasoning: 2. Four real bear bullets vs. four real bull bullets; "constructive" is defensible but the verdict_action's "potential target of SMA-20 at 210.30" is a directional error (close already above that level - see headline finding #1). "RSI 64.7 indicating potential for further growth" misses that the report shows RSI dropped from 76.7 to 64.7 (cooling from overbought), which is mean-reversion in progress, not a green light.
- Actionability: 1. Real levels but the trigger is incoherent ("target" of an already-crossed level).
- Disclosure: 0.

### Sample 2 - UNH thesis

> **Setup:** The central question for UNH is whether its vertical integration and strong market position in the healthcare industry can drive growth and profitability, despite potential risks such as Medicare Advantage rate notices and PBM regulatory scrutiny (source: company). The company's close above SMA-50 and SMA-20, as well as its overbought RSI-14, suggest a bullish trend (source: technical). However, the fundamental report indicates a neutral signal, with mixed growth and profitability metrics (source: fundamental).
>
> **Bull Case:**
> - Close above SMA-50 (320.23) by +22.99% - uptrend (source: technical)
> - RSI-14: 77.0 - overbought (above 70 threshold; oversold ≤ 30) (source: technical)
> - MACD(12/26/9): MACD +21.71 above signal +20.82 (histogram +0.88, expanding) - bullish (source: technical)
>
> **Bear Case:**
> - Warren Buffett's Berkshire Big Shakeup Continues: UnitedHealth Stake Completely Sold (source: news)
> - Berkshire Hathaway has fully exited its position in UnitedHealth Group (NYSE:UNH) under Greg Abel's leadership (source: news)
>
> **Verdict:** cautious - Close above SMA-20 (369.53) by +6.58% - potential support level (source: technical)

- Reasoning: 2. Asymmetric (3 bull / 2 bear) - correct. "Cautious" stance reflects the Buffett-exit weight. **But** overbought RSI as a bull bullet is a logic error (headline finding #2). The two bear bullets are also slightly redundant (both restate the Buffett exit) - should be one bullet.
- Actionability: 2. 369.53 is a real support level; framing as "potential support level" is correct (sanity check passed). No trigger, no time horizon, no "watch X" - directional but not specific.
- Disclosure: 0.

### Sample 3 - AAPL quick_fact

> The RSI for AAPL is 75.9, which is considered overbought (source: technical). (`cited_value: "75.9"`, `source: technical`)

- Reasoning: 3. Within the shape it's expected to fill, this is exactly what a desk would say.
- Actionability: 2. Regime label ("overbought") implicitly suggests caution but no trigger.
- Disclosure: 0.

### Sample 4 - NVDA vs AAPL comparison

> **NVDA:** NVIDIA designs GPUs, data-center accelerators, and the CUDA software stack. The stock shows a bullish technical signal with an RSI of 64.7 and a P/E of 45.45 (source: technical, fundamental).
> - P/E 45.45, RSI-14 64.7, EV/EBITDA 40.97, Price/Sales 80.11
>
> **AAPL:** Apple designs and sells iPhone, Mac, iPad, Wearables, and Services. The stock shows a bullish technical signal with an RSI of 75.9 and a P/E of 35.97 (source: technical, fundamental).
> - P/E 35.97, RSI-14 75.9, EV/EBITDA 27.81, Price/Sales 39.66
>
> **Differences:** NVDA trades at a richer multiple with a higher P/E and EV/EBITDA compared to AAPL. AAPL shows stronger momentum with a higher RSI-14. NVDA carries more valuation risk with a higher Price/Sales ratio.

- Reasoning: 2. Differences paragraph respects the "no new numbers" rule. But "AAPL shows stronger momentum with higher RSI-14" misreads regimes (headline finding #4). Calling AAPL "bullish technical signal" while its RSI is 75.9 (overbought) is also questionable framing - copied the technical report's SIGNAL label verbatim instead of reasoning over it.
- Actionability: 1. No "I'd prefer X over Y for Z reason", no watch metrics, no triggers. Just a contrast.
- Disclosure: 0.

### Sample 5 - TSLA thesis (mis-routed from focused-technical)

> **Setup:** The central question for TSLA is whether the company can maintain its growth momentum in the face of increasing competition in the EV market, with key risks including auto gross margin compression and demand softness in key EV markets (source: company). The technical report indicates a close above SMA-50 and SMA-20, suggesting an uptrend (source: technical). The fundamental report shows a P/E of 410.62, indicating a rich valuation (source: fundamental).
>
> **Bull Case:**
> - RSI-14 at 58.0, indicating a neutral momentum with room for growth (source: technical)
> - MACD above signal line, indicating a bullish trend (source: technical)
> - Revenue growth of 15.78% YoY, indicating strong growth (source: fundamental)
> - Governor Gavin Newsom's $1 Billion EV Incentive Program could benefit Tesla Semi (source: news)
>
> **Bear Case:**
> - P/E of 410.62, indicating a rich valuation (source: fundamental)
> - Auto gross margin compression from price cuts, a key risk (source: company)
> - FSD / robotaxi timeline slippage, a key risk (source: company)
> - Tesla's recent price increases may impact demand (source: news)
>
> **Verdict:** mixed - Close above SMA-50 of 386.78, with a neutral RSI-14 of 58.0, suggests a potential buying opportunity, but high P/E of 410.62 and auto gross margin compression risks warrant caution (source: technical, fundamental)

- Reasoning: 3. Symmetric 4/4. "Mixed" stance is correctly chosen - bull and bear genuinely balance. Verdict action hedges both sides explicitly. This is the best sample in the batch.
- Actionability: 3. Three different anchors (SMA-50 386.78, RSI 58.0, P/E 410.62) and an explicit hedge.
- Disclosure: 1. "Warrant caution" is implicit hedge but no explicit "not advice."

### Sample 5b - TSLA focused-technical (re-fired with "Give me a technical analysis on TSLA")

> **Focus:** technical
>
> **Summary:** TSLA's price action is in an uptrend, with the close above SMA-50 by +9.17% (source: technical) and above SMA-20 by +5.21% (source: technical). The RSI-14 is 58.0, indicating a neutral momentum (source: technical), while the MACD is +15.32 above the signal, which is bullish (source: technical). The overall signal is bullish, with 3/3 indicators agreeing (source: technical).
>
> - TSLA's close is above SMA-50 by +9.17% (source: technical)
> - TSLA's RSI-14 is 58.0, indicating a neutral momentum (source: technical)
> - The MACD is +15.32 above the signal, which is bullish (source: technical)
>
> **Cited values:** Close 422.24 | RSI-14 58.0 | MACD +15.32

- Reasoning: 2. Recitation, not reasoning. Quotes the SIGNAL aggregate line ("3/3 indicators agreeing") despite the prompt forbidding it (headline finding #7).
- Actionability: 1. No trigger, no watch level. By prompt design, but the user asking for a technical analysis didn't ask to lose the verdict.
- Disclosure: 0.

### Sample 6 - META conversational

> I can provide information on US public equities, covering various aspects such as technical, fundamental, and news-related data. I can offer insights into specific companies and compare them. I'm here to help with your equity-related questions.
>
> Suggested next questions:
> - What can you tell me about the technical aspects of NVDA?
> - Can you compare the fundamental data of AAPL and MSFT?
> - How does the news sentiment look for GOOGL?

- Reasoning: - (not applicable to shape).
- Actionability: - (not applicable to shape).
- Disclosure: 1. "Information" / "insights" / "help" - implicit informational framing; no explicit disclaimer.

## Fixes - see roadmap

The seven prompt / classifier / schema fixes derived from these findings are tracked as items **B-1 through B-7** in [`docs/AI Engineering improvement plan.md`](../AI%20Engineering%20improvement%20plan.md), each with drafted prompt text, file:line pointers, and eval-pairing notes. Mapping:

| Finding (this doc) | Roadmap item |
|---|---|
| Overbought metrics as bull points (samples 2, 4) | B-1 |
| FOCUSED quotes SIGNAL aggregate line (sample 5b) | B-2 |
| Prior-session momentum delta ignored (sample 1) | B-3 |
| Verdict action confuses support and target (sample 1) | B-4 (deterministic tripwire - directional sanity) |
| Verdict action state-shaped not trigger-shaped (samples 1, 2, 5) | B-8 (cousin of B-4 - shape sanity) |
| Intent classifier missed focused ask (sample 5) | B-5 |
| Focused shape strips actionability by design | B-6 (product decision) |
| No financial-advice disclaimer | B-7 |

## Out of scope / not measured here

- Numeric grounding - already covered by `packages/agent/src/agent/evals/hallucination.py`.
- Prompt-injection resistance - covered by QNT-161 (validate_chat_message).
- Latency / cost - unrelated to analyst quality.
- The conversational-shape suggestions' phrasing (sample 6 suggested asks against tickers AAPL, MSFT, NVDA, GOOGL - coverage feels fine but wasn't graded).

## Reproduction

```bash
# Stack up
make tunnel & make dev-litellm & make dev-api &
# Wait for: curl -s http://localhost:8000/api/v1/health → 200

# Sample (one per intent):
for spec in \
  'NVDA|Give me a balanced thesis on NVDA - is the AI capex story still intact?' \
  'UNH|Bull and bear case for UNH right now?' \
  'AAPL|What is AAPL RSI?' \
  'NVDA|Compare NVDA and AAPL on valuation and momentum.' \
  'TSLA|Walk me through TSLA technical setup.' \
  'TSLA|Give me a technical analysis on TSLA.' \
  'META|Hi, what can you do?'; do
  ticker="${spec%%|*}"; msg="${spec#*|}"
  curl -N -s -X POST http://localhost:8000/api/v1/agent/chat \
    -H 'Content-Type: application/json' \
    -d "{\"ticker\":\"$ticker\",\"message\":\"$msg\"}"
  echo "---"
done
```

Snapshot captured 2026-05-18; market data was as-of 2026-05-15 in the live reports.
