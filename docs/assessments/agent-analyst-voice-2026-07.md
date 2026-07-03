# Agent answer quality — senior-analyst voice v6

**Date:** 2026-07-03
**Ticket:** QNT-303
**Method:** Live **prod-trace** sampling. 1,400 GENERATION observations over a
45-day window (2026-05-27 → 2026-07-03) were pulled read-only from the Langfuse
`/api/public/observations` API (ADR-019: GETs only, zero new spans), grouped by
`traceId`, and bucketed by intent (parsed from the `classify` node output). The
`narrate` node output — the streamed analyst-voice bubble (ADR-020, QNT-285
BLUF) — is the primary surface graded here; the structured `synthesize` output
is read alongside for grounding. This is the v6 successor to the 2026-05-18 v1
round (`agent-analyst-quality-2026-05-18.md`); it audits the intents that
shipped *after* v1 and had never had a voice pass: exploration, lean-comparison
narration, followup narrative-only, clarify lead-ins, and the QNT-276
retrieved-evidence narrative-only paths.

## What this doc is — and isn't

The **forensic snapshot** behind the QNT-303 rule decisions. It captures the
verbatim prod outputs, the per-signature scores, and the reproduction so a
future re-run produces a comparable artifact. **Assessment first**: the six
candidate senior-analyst signatures (D-1..D-6) were *hypotheses* to confirm
against samples, not pre-commitments. Only the signatures the samples actually
show missing were shipped; the rest are documented here with the evidence for
deferring. When you re-run, add a sibling `agent-analyst-voice-<date>.md`; do
not edit this one.

## Per-intent narrate corpus (recent window, ≥ 2026-06-25)

| Intent | Recent narrates | BLUF bold-call opens | Notes |
|---|---:|---:|---|
| thesis | 3 | 3/3 | Clean BLUF; tension named; no falsifier |
| followup | 5 | 5/5 | Clean BLUF; tension present |
| news | 13 | 9/13 | **4 retrieved-evidence narrations bury the read mid-paragraph** |
| comparison | 1 | 1/1 | Narrate already emits a relative lean |
| technical (focused) | 1 | 1/1 | Terse label BLUF ("**Sideways**") |
| quick_fact | — (no narrate) | — | Structured only; value+regime present |

(Full corpus also spans older pv=6–15 samples back to 2026-05-27; the recency
cut isolates current-prod voice from pre-BLUF history.)

## Candidate-signature scorecard (D-1..D-6)

| Signature | Verdict | Evidence | Action |
|---|---|---|---|
| **D-1 Falsifier** | **MISSING** | **0 / 63** recent narrations name what would change the view | **SHIPPED** — narrate rule 8, scoped to thesis/comparison |
| D-2 Tension-naming | Largely present | Thesis/followup/comparison name the conflict ("rich valuation … complicate the picture") | Not shipped — samples don't support a new hard rule |
| D-3 Comparison bottom line | Covered by narrate | Comparison narrate already leads with a relative call ("**More cautious on NVDA relative to AAPL**") | **Deferred** — product decision; narrate covers it, structured-card change not approved |
| D-4 Quick-fact "so what" | Largely present | quick_fact answers carry value + regime word ("RSI-14 … is 41.2, which is neutral") | Not shipped here — prior-session grounding overlaps QNT-296 |
| D-5 Conviction / coverage | Unaudited (no degraded turns) | No news-unavailable / degraded-coverage narration in the 45-day window | **Deferred** — pairs with QNT-299; no graded-poor sample to justify wiring |
| **D-6 Filler ban** | Clean now, no guard | **0** filler hits in recent narrations, but no permanent tripwire existed | **SHIPPED** — deterministic eval-path gate (no-regret) |

## Headline findings

1. **Falsifier is universally absent (D-1).** Across 63 recent prod narrations
   spanning thesis, followup, news, and comparison, **zero** name a printed
   threshold whose breach would flip the call. The `Watch:` close names a
   *catalyst to monitor* ("the upcoming earnings report"), which is a forward
   calendar item, not a falsifier tied to a level the report already prints.
   This is the strongest missing senior-analyst signature and the primary
   shipped rule. It is ADR-003-safe: the rule anchors on a regime LABEL the
   payload already prints (Uptrend/Premium/an RSI band) and forbids inventing a
   number. **Scope:** gated to **thesis and comparison** only — the two shapes
   that reliably fetch the technical + fundamental reports that print those
   labels. News is narrative-only and a followup can elaborate a news thread, so
   neither carries a printed threshold; an initial draft that included news made
   the model fabricate a *"200-day moving average"* no report stated (a
   non_hallucination regression, caught by the clean-window run below and fixed
   by narrowing the scope + steering the anchor to labels not raw levels).

2. **Tension-naming is already a strength (D-2).** The BLUF narrate reliably
   names the conflict and which side is weighted — e.g. AAPL: *"the uptrend …
   is tempered by its premium valuation, with a P/E of 35.27 … introduce
   caution."* No new rule warranted; the D-1 rule reinforces this by asking
   which side would have to break.

3. **Comparison already delivers a relative lean (D-3).** The candidate framed
   the rich comparison as ending with *no* call. In prod, the narrate wrapper
   already opens with a relative preference: *"**More cautious on NVDA relative
   to AAPL** … makes Apple appear more stable."* The structured card's
   `differences` paragraph deliberately stays contrast-only
   (`COMPARISON_SYSTEM_PROMPT`: "Do not extend to a buy/sell recommendation").
   D-3 was flagged for explicit product approval; absent approval, and given
   narrate already covers the relative lean, the structured-card change is
   **deferred** (no prompt edit).

4. **Quick-fact carries the "so what" already (D-4).** Metric lookups pair the
   value with its regime word: *"The RSI-14 for NVDA is 41.2, which is neutral
   (source: technical)."* The remaining half of the candidate — prior-session
   direction — is a runtime-grounding change that overlaps **QNT-296**
   (quick_fact runtime numeric grounding); deferred there to avoid two tickets
   editing the same shape.

5. **No degraded-coverage turns to grade (D-5).** The candidate asks the
   narrator to hedge on degraded turns ("with news unavailable, this is a
   partial read"). The 45-day window contained no degraded-coverage narration
   to grade the failure against. Without a graded-poor sample, wiring
   composite confidence/coverage into narrate would be a speculative edit
   (violates AC2's "no taste-only edits"). Deferred; pairs with QNT-299's
   degradation note when a degraded sample surfaces.

6. **Filler is clean but unguarded (D-6).** Zero banned-filler hits in the
   recent corpus — the ADR-020 voice block's "no padding" instruction is
   holding. But there was no *deterministic* guard, so a future prompt
   regression could reintroduce "it's important to note" / a leading "Overall,"
   with only the LLM judge (which misses soft filler) between it and prod. A
   banned-phrase tripwire is a no-regret permanent regression guard and ships
   regardless of the current clean reading (AC3).

## Per-intent verbatim samples

### thesis — AAPL (pv=15, 2026-07-02) — BLUF clean, no falsifier

> **Constructive, but priced for it.**
>
> The setup for AAPL looks constructive, driven by a strong fundamental
> backdrop with revenue growth of +16.60% YoY and a growing Services segment
> (source: company). However, the rich valuation, with a P/E of 35.27, and
> technical mixed signals, including a bearish MACD, complicate the picture
> (source: fundamental). …
>
> Watch: The upcoming earnings report and management's guidance on AI feature
> rollout and Services momentum.

- BLUF: 3. Bold call, one blank line, synthesis prose.
- Tension (D-2): present — "rich valuation … complicate the picture."
- Falsifier (D-1): **absent.** The Watch line is a calendar catalyst, not a
  printed level whose breach flips "constructive." A falsifier here would read
  "…turns cautious if it loses the SMA-50 the report prints."

### news — NVDA retrieved-evidence (2026-06-25) — BLUF **missed**

> Nvidia's recent partnership with SLB, a collaboration that's been in place
> for roughly two decades, has taken center stage … The picture looks
> constructive, with Nvidia's shares having …

- BLUF: 1. The read ("constructive") is buried in sentence three instead of
  leading as a bold call. 4 of 13 recent news narrations show this pattern,
  all on the retrieved-evidence "any recent news" path (QNT-276). The bold-call
  contract already exists in `NARRATE_SYSTEM_PROMPT`; this is a compliance-rate
  gap, not a missing rule — documented, not separately re-ruled, to avoid
  prompt bloat that risks regressing the 9/13 that comply.

### news — NVDA retrieved-evidence (2026-07-02) — BLUF clean, ids anchored

> **Constructive, driven by new partnerships and platform announcements.**
>
> The recent announcement of the NVIDIA Vera Rubin platform … (source: finnhub
> R1). Additionally, the partnership with Palantir … (source: finnhub R3). …
>
> Watch: The deployment of Vera-Rubin … (source: finnhub R2).

- BLUF: 3. Retrieved-evidence ids (R1/R3/R2) carried into citations correctly
  (QNT-301 contract holding on the narrate surface).

### comparison — NVDA vs AAPL (2026-07) — relative lean already present (D-3)

> **More cautious on NVDA relative to AAPL.**
>
> … NVIDIA's explosive growth … starkly contrasts with Apple's more stable
> growth profile … However, NVIDIA's technical setup looks more challenged …
> This trade-off … makes Apple appear more stable, albeit with a lower growth
> profile.

- The relative preference D-3 asked for is already here in the narrate wrapper.

### followup — META cloud (2026-07-02) — BLUF + tension clean

> **Meta's cloud business plans are a double-edged sword.**
>
> The company's move into cloud computing … could be a lucrative new business …
> However, this also poses risks to AI infrastructure stocks … The fundamental
> picture shows an Inline label, with a P/E of 19.07 … but the technical trend
> is mixed …

- Tension (D-2): present and well-weighted. Falsifier (D-1): absent.

### quick_fact — NVDA / MSFT (2026-07) — value + regime present (D-4)

> The RSI-14 for NVDA is 41.2, which is neutral (source: technical).

> The latest management guidance from MSFT earnings is revenue growth of
> 18.30% YoY (source: fundamental) …

- The value never lands bare — a regime word or YoY frame accompanies it.

## Rules shipped (each traces to a finding above)

| Rule | Finding | Where | Fixture |
|---|---|---|---|
| **D-1 falsifier** | Finding 1 (0/63) | `NARRATE_FALSIFIER_RULE`, appended for `_FALSIFIER_INTENTS` = {thesis, comparison} in `build_narrate_prompt` | `test_analyst_voice.py::test_falsifier_rule_present_for_label_bearing_intents` (+ absent where no printed threshold, + ADR-003-safe) |
| **D-6 filler tripwire** | Finding 6 (no guard) | `agent/analyst_voice.py::find_filler` + `_apply_deterministic_filler_gate` in `dialogue_eval.py` | `test_analyst_voice.py::test_find_filler_*` + `test_filler_gate_*` |

`prompt_version` is computed from the prompt text (`agent.prompt_version`), so
the single `NARRATE_FALSIFIER_RULE` edit bumps it exactly once.

## Deferred (with reason)

- **D-2** — already a strength; no new rule.
- **D-3** — narrate already leads with the relative lean; structured-card change
  needs explicit product approval. **Approved 2026-07-03 and shipped as a
  QNT-303 follow-up** (see the follow-up section below).
- **D-4** — value+regime already present; prior-session grounding belongs to
  **QNT-296** (same shape).
- **D-5** — no degraded-coverage sample in-window to grade against; pairs with
  **QNT-299** when one surfaces.
- **News BLUF compliance (4/13)** — the bold-call rule already exists; a
  compliance-rate gap on the retrieved-evidence path, documented not re-ruled.

## Reproduction

```bash
# Read-only prod-trace harvest (Langfuse GETs only, ADR-019):
#   scratchpad harvest_traces.py pages /api/public/observations (type=GENERATION),
#   groups by traceId, buckets by classify-node intent, dumps narrate+synthesize.
# LANGFUSE_* keys from .env; base URL must be the US host (QNT-61 region trap).
uv run python -m agent.evals.langfuse_baseline --days 45   # per-node token/latency companion
```

Snapshot captured 2026-07-03; window 2026-05-27 → 2026-07-03.

## Appendix — before/after (D-1) + clean-window verification (AC4)

Generated live against the real narrate model (`groq/llama-3.3-70b` via the
local LiteLLM proxy, temperature 0) with the falsifier rule OFF vs ON.

**thesis (AAPL)**

- BEFORE: *"**Neutral** … The technical setup is generally upbeat but shows a
  slightly bearish MACD … Watch: App Store regulatory developments."* — no
  falsifier.
- AFTER: *"**Neutral, for now.** … the stock maintaining its uptrend and
  closing above the SMA-50 … **The read holds while the trend label stays
  Uptrend.**"* — label-anchored falsifier, no invented number.

**comparison (NVDA vs AAPL)**

- BEFORE: *"…The tension between these two setups complicates the comparison …
  Watch: NVDA's ability to break above its SMA-50."* — no falsifier.
- AFTER: *"…**The read holds while NVDA's growth remains strong; it flips if
  NVDA's RSI drops into oversold territory.**"* — anchored on the printed RSI
  band.

**Paired clean-window run** — the same 5 fixtures (thesis/followup/news + a
technical control) replayed on `main` (baseline) and on this branch (candidate),
same session, agent temp 0, judge = OpenRouter per ADR-023. The QNT-215
"no-regression" contract is a paired delta, so both runs are shown:

| Axis | baseline (main) | candidate (branch) | delta |
|---|---:|---:|---:|
| non_hallucination | 1.000 | 1.000 | +0.000 |
| voice_match | 0.840 | 0.910 | +0.070 |
| helpfulness | 0.810 | 0.840 | +0.030 |
| analyst_likeness | 0.760 | 0.780 | +0.020 |
| exploration_quality | 0.620 | 0.670 | +0.050 |
| composite | 0.806 | 0.840 | +0.034 |

**Every axis is flat or up** — no guardrail regresses (the QNT-215 gate's
`non_hallucination`/`helpfulness`/`voice_match` are all >= baseline), and the
filler gate produced no false positives. An earlier D-1 draft scoped to news had
driven `msft-news-what-matters` to `non_hallucination=0.0` by fabricating a
"200-day moving average"; that was caught pre-merge and fixed by narrowing the
falsifier to thesis/comparison — the paired run above is post-fix. (Judge
single-run scatter on `analyst_likeness` remains; the paired design cancels the
shared fixture-difficulty term, so the deltas are the trustworthy quantity.)

## Follow-up (2026-07-03, same-day, post-ship)

**D-3 shipped (product-approved).** The comparison `differences` paragraph now
closes with one RELATIVE-preference sentence between the two named tickers
("at current levels, AAPL screens as the more balanced setup on
valuation-vs-momentum"), never an absolute buy/sell, tied to the printed aspect
labels, no new number. Pinned by `test_comparison_prompt_closes_with_relative_preference`.

**Watch-vs-falsifier competition (confirmed, deferred as a design decision).**
A controlled A/B (n=10 each, temp 0.3, live `groq/llama-3.3-70b`) confirmed the
D-1 falsifier crowds out the QNT-285 "Watch:" close on thesis — Watch went from
~7/10 to ~4/10 while the falsifier took the closing slot. Both are optional
*closes* and the falsifier ("the read holds while Uptrend holds; flips if RSI
rolls over") is semantically a terminal sentence, so it wins the slot. Prompt
ordering (Watch-last) and explicit "keep Watch as the final line" wording did
NOT reliably restore it on this model (Watch stayed ≤1/10). This is a
genuine product trade-off — the falsifier is arguably a *stronger* close than a
generic "Watch: the earnings print" — not a mechanical bug, so it is left for a
product decision rather than shipped with a non-working fix. Options on the
table: (a) accept the substitution; (b) merge the two into one combined close
("read holds while Uptrend holds — watch the print for confirmation"); (c) make
the falsifier body-only and rarer.
