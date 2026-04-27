# Free-tier LLM bench — April 2026

**Date**: 2026-04-26 (initial bench), 2026-04-27 (reference re-run)
**Linear**: [QNT-129](https://linear.app/noahwins/issue/QNT-129) — bench harness lives at `python -m agent.evals --model <alias>`, raw rows in `packages/agent/src/agent/evals/history.csv`. [QNT-138](https://linear.app/noahwins/issue/QNT-138) — clean reference row on a fresh Groq TPD bucket.
**Outcome**: replaced `qwen/qwen3-32b` (12/16 hallucination_ok) with `meta-llama/llama-4-scout-17b-16e-instruct` (16/16 + best judge / latency) as the fallback behind `equity-agent/default`; **kept Llama-3.3-70B as default** after QNT-138's clean re-run showed Scout's pre-registered promotion thresholds were not met. See [Recommendation](#recommendation).

---

## Why this exists

The QNT-128 retro found that the production fallback model (Qwen3-32B) was promoted on a *capacity* signal — Groq's 500K TPD ceiling vs Llama-3.3-70B's 100K — without checking quality first. After the fact, a re-run on the QNT-67 goldens showed Qwen3-32B regressed from 15/16 to 11/16 hallucination_ok and leaked `<think>` reasoning blocks into theses. Wrong sequence: capacity then quality, instead of quality then capacity.

This bench fixes the order. Every free-tier candidate that could plausibly sit in our `equity-agent/default → fallback-*` chain runs the same 16-record golden set under the same harness, against the same reference theses. The output is a recommendation that points at the next slot in the chain (or no slot — leave the chain alone) with measured numbers behind it.

The doc is also the portfolio artifact: a recruiter clicking through the README ([QNT-66](https://linear.app/noahwins/issue/QNT-66)) should be able to land here in two clicks and answer *"why this model and not that one"* without reading code.

---

## Methodology

Each candidate gets one full sweep against `packages/agent/src/agent/evals/goldens/questions.yaml` (16 records, 10 tickers). The sweep runs through `python -m agent.evals --model equity-agent/bench-<name>`; the `--model` flag (added with this ticket) plumbs the alias through the module-level `set_model_override()` so plan, synthesize, and the LLM-as-judge call all hit the model under test. **No fallback chain on bench aliases** — every record's score reflects the model under test, not a silent fall-through to a different model.

**Per-alias rate caps.** Each sweep is ~48 LLM calls (3 per record × 16: plan + synthesize + judge). Fast models (gpt-oss-*) would otherwise burst above the published RPM and 429 instead of completing. The fix is server-side: `rpm`/`tpm` set at ~80% of each provider's published free-tier cap on the bench alias itself. LiteLLM queues over-cap requests instead of returning 429, so the eval sees latency, not failures. See `litellm_config.yaml` for the exact caps.

The reference `equity-agent/bench-llama3-70b` alias mirrors the production default's underlying model (`groq/llama-3.3-70b-versatile`) but has no fallback chain, so the reference row is a fair within-bench comparison line on the same `prompt_version` as every candidate. This matters because the QNT-128 baseline (`20260425T142759Z-f1aa25`) was on a pre-QNT-133/QNT-136/QNT-137 prompt and the post-QNT-133 four-section thesis structure shifts cosine distances meaningfully — re-using the QNT-128 numbers as the baseline would have anchored every candidate to the wrong target.

**What's measured per row.**

| Metric | Source | What it actually means |
|---|---|---|
| `hallucination_ok N/16` | `evals.hallucination` | Number of records where every numeric claim in the thesis appears verbatim (modulo `$`/`,`/`%` formatting and YoY sign-flip idioms) in one of the gathered reports. Hard contract — anything <16 means the model invented numbers. |
| `tool_call_ok N/16` | `evals.tool_calls` | Number of records where every `expected_tools` entry actually fired during gather. Hard contract — anything <16 means the planner skipped a required report (or the synthesize call errored out and the run aborted). |
| `avg judge_score` | `evals.judge` (LLM-as-judge, **same model under test**) | 0–10 rubric over the reference thesis. Soft signal. Self-judging biases this in unknowable directions per-model, so it's most useful for in-model trend (rerun-vs-rerun) and least useful for cross-model ranking. |
| `avg cosine` | `evals.similarity` (sentence-transformers all-MiniLM-L6-v2) | Embedding cosine vs the reference thesis. Soft, but the embedder is fixed across runs so this **is** comparable across candidates. |
| `p50 elapsed_ms` | history.csv `elapsed_ms`, median | End-to-end per-record latency including HTTP tool-fetches and the synthesize call. Not a clean LLM-only number, but it's apples-to-apples across candidates because the tool layer is identical. |
| `total tokens` | Langfuse `metrics` API, summed across the bench window | Sum of input + output tokens across all generation spans for the alias. Caveat below. |

**Known measurement gap — synthesize tokens missing.** The synthesize node calls `llm.with_structured_output(Thesis)`, which returns the parsed Pydantic model rather than the raw `AIMessage`. `traced_invoke` reads `model_name` and `usage_details` from `AIMessage.response_metadata` only, so synthesize generations land in Langfuse with `providedModelName=null` and `usage_details=null`. The reported `total tokens` column below is therefore **plan + judge + (synthesize errors that surfaced as raised exceptions)** — a lower bound on actual consumption. Also explains why Qwen3-32B and Gemma-4-31B show inflated output token counts despite producing terse final theses: their plan/judge responses leak reasoning (`<think>...</think>` for Qwen, freeform scratchpad for Gemma) which shows up in token usage. Follow-up: switch to `with_structured_output(..., include_raw=True)` and read tokens off `response['raw']`. Tracked separately — out of scope for QNT-129's bench-only ticket.

**What's NOT measured here.** Latency under load, multi-turn coherence, function-calling depth, vision, code generation. Bench is narrowly the QNT-67 hallucination + structural-grounding contract; promoting to a slot in the chain off this bench commits us to *this thesis task*, not "best general-purpose free-tier LLM".

---

## Candidates

Eight models — seven free-tier candidates plus the production default as a calibration line. All exist on their providers as of 2026-04-26 (verified by hitting `https://api.groq.com/openai/v1/models` and `generativelanguage.googleapis.com/v1beta/models`).

| Model | Provider | LiteLLM alias | Free-tier limit | Going-in status |
|---|---|---|---|---|
| `llama-3.3-70b-versatile` | Groq | `equity-agent/bench-llama3-70b` | 100K TPD | **Reference** — current production default |
| `openai/gpt-oss-120b` | Groq | `equity-agent/bench-gptoss120b` | 200K TPD | Smoke-tested clean; Harmony format keeps reasoning out of `.content` |
| `openai/gpt-oss-20b` | Groq | `equity-agent/bench-gptoss20b` | 200K TPD | Smoke-tested clean; check if "good enough" for fallback duty |
| `meta-llama/llama-4-scout-17b-16e-instruct` | Groq | `equity-agent/bench-llama4scout` | 500K TPD | Untested; Llama lineage |
| `qwen/qwen3-32b` | Groq | `equity-agent/bench-qwen3-32b` | 500K TPD | Already 11/16 (run `7d54ca`); included for reproducibility on current prompt |
| `gemma-4-31b-it` | Google AI Studio | `equity-agent/bench-gemma4-31b` | 1.5K RPD, unlimited TPM | Smoke-tested poorly (scaffolding leak); confirm on goldens |
| `gemma-3-27b-it` | Google AI Studio | `equity-agent/bench-gemma3-27b` | 14.4K RPD | Untested; biggest Gemini-side capacity |
| `gemini-3.1-flash-lite-preview` | Google AI Studio | `equity-agent/bench-gemini31flashlite` | 500 RPD, 250K TPM | Untested; best preview Gemini option |

Excluded from candidate list: `gemini-2.5-flash` / `gemini-2.5-flash-lite` (20 RPD on this account — one sweep would already burn 80% of the daily ceiling).

---

## Results

Sorted by hallucination_ok (the only hard contract that doesn't have a vacuous-pass failure mode), then by cosine.

| Model | hallucination_ok | tool_call_ok | judge | cosine | p50 elapsed | total tokens (lower bound) | Notes |
|---|---|---|---|---|---|---|---|
| **llama-3.3-70b (reference, default)** | **16/16** | **16/16** | 4.69 | **0.364** | 8 398 ms | 9 071 | QNT-138 clean re-run on fresh 100K TPD bucket. Best cosine in the bench. Wall-clock p50 dominated by per-alias bench TPM throttle (5 K TPM queues later plan calls); per-call Langfuse latency p50 is 311 ms. The original TPD-truncated sweep is retained in `history.csv` as audit trail |
| llama-4-scout-17b-16e-instruct (fallback) | 16/16 | 16/16 | **4.94** | 0.342 | **836 ms** | 8 064 | Best judge and lowest wall-clock latency. Loses to Llama on cosine by 0.022 (Llama's full-row clean number, not the QNT-129 truncated 0.336) |
| gpt-oss-120b | 16/16 | 16/16 | 1.69 | 0.296 | 12 525 ms | 15 287 | Refuses to interpret reports it gathered (see qualitative notes) |
| gpt-oss-20b | 16/16 | 16/16 | 0.94 | 0.271 | 13 417 ms | 16 551 | Same defensive failure mode, more pronounced |
| gemma-4-31b-it | 16/16 | 16/16 | 1.81 | 0.284 | 37 882 ms | 23 693 | Slow (per-call latency 16s p50); freeform scratchpad leaks into output tokens |
| gemini-3.1-flash-lite-preview | 13/16 | 13/16 | 3.15 | 0.261 | 4 671 ms | 8 753 | RateLimitError on 3/16 records mid-sweep — published 15 RPM not paceable down to actual upstream cap |
| qwen/qwen3-32b | 12/16 | 16/16 | 1.44 | 0.296 | 25 635 ms | 21 272 | Hallucinates magnitudes (4 records); function-calling failures on META; `<think>` leakage |
| gemma-3-27b-it | 16/16 (vacuous) | 16/16 | 6.12 | 0.000 | 1 600 ms | 5 358 | **Disqualified** — no JSON mode → every synthesize call returned 400 → empty thesis on every record. `hallucination_ok` is vacuously true on empty input; cosine 0.000 is the giveaway |

### Per-model qualitative notes

**llama-3.3-70b (reference, default).** QNT-138 re-ran the reference sweep on a fresh Groq 100K TPD bucket (run_id `20260427T122411Z-8ebb34`). 16/16 on every hard contract; cosine 0.364 is the best in the bench; judge 4.69 is second only to Scout. The wall-clock p50 of 8 398 ms is dominated by the per-alias bench TPM throttle on the bench harness — `bench-llama3-70b` is capped at 5 K TPM (~80% of Groq's 6 K free-tier TPM on 70B), so plan calls in later records queue rather than 429. Per-call Langfuse latency p50 is 311 ms (vs Scout's 169 ms): Scout is genuinely 2× faster per call, but not 10× as the wall-clock suggests. The original TPD-truncated sweep (`20260426T151730Z-5d616e`, 9 clean + 7 zero-cosine records) is retained in `history.csv` for reproducibility; the canonical reference row above uses the QNT-138 clean run.

**llama-4-scout-17b-16e-instruct (fallback slot — confirmed by QNT-138).** Wins judge (4.94 vs 4.69) and per-call latency (169 ms vs 311 ms p50). Loses cosine on the clean reference row by 0.022 (0.342 vs 0.364) — within run-to-run noise but consistent direction across both runs. The QNT-129 ship rationale (5× the default's TPD ceiling, strictly better than the Qwen3-32B it replaced on every measured signal, same Groq client path) stands. QNT-138 closed-out the deferred default-promotion question against the pre-registered decision rule (cosine lead ≥ 0.02 OR judge lead ≥ 2.0): cosine lead is **negative** (Llama leads), judge lead is +0.25 — both well below the threshold. Scout stays in the fallback slot.

**gpt-oss-120b / gpt-oss-20b (defensive failure).** Both score 16/16 on the hard contracts but produce theses like *"the supplied technical report does not provide any overbought indicator values such as RSI or moving-average levels"* — when the report does in fact contain RSI, MACD, and SMA stack data. The model parses the report but won't draw conclusions from it. Failure mode is the safe one (won't hallucinate) but useless for the thesis task. Both gpt-oss variants share this pattern; 120b is marginally less terse than 20b but the same posture.

**qwen/qwen3-32b (confirms QNT-128 finding).** 12/16 hallucination_ok on the current prompt (vs the QNT-128 result of 11/16 on the older prompt — within noise). Failure rows invent magnitudes for AAPL P/E (`192.50`, `62`, `70`, `8.7` — none in the report), JPM EPS-class numbers (`145.00`, `148.32`, `200`), and UNH headlines (`385.20`). Also surfaces a Groq `tool_use_failed` error on synthesize for META (function-call mode collapses on its structured-output path). Inflated 21 272 output tokens vs llama-4-scout's 8 064 reflects `<think>` block leakage into plan/judge calls. Same model, same evidence pattern, same recommendation as QNT-128: keep out of the production chain.

**gemma-4-31b-it.** Passes hard contracts but is the slowest candidate at 37 s p50 elapsed per record (gemma's per-call latency on Google AI Studio is the binding constraint, not the model itself — Groq-hosted Llama-3.3-70B at 70B class is faster than Google-hosted Gemma at 31B). Output quality is below scout/Llama on judge and cosine. No reason to prefer it given the latency ceiling.

**gemma-3-27b-it (incompatible).** Every synthesize call returns `400 — JSON mode is not enabled for models/gemma-3-27b-it`. The `with_structured_output(Thesis)` path uses LiteLLM's JSON-mode shim, which Gemma-3 doesn't support. All 16 records ended with empty theses; the harness's `hallucination_ok` vacuously passes (no numeric claims to check), but cosine 0.000 across the board is the unambiguous tell. Disqualified for our schema-driven synthesize node. Workaround would be a prompt-based JSON path — out of scope for this bench.

**gemini-3.1-flash-lite-preview.** Three records errored out with `RateLimitError` despite the alias having `rpm: 12` (80% of the published 15 RPM). Either the published RPM is rounded up from a tighter per-second QPS, or the free-tier account has stricter limits than the docs advertise. Quality on the 13 records that did complete is middle-of-pack (cosine 0.261, judge 3.15) — not bad enough to disqualify on quality, but the rate-limit unreliability disqualifies it for fallback duty (a fallback that 429s is worse than no fallback).

---

## Recommendation

**Replace `equity-agent/fallback-qwen3` with `meta-llama/llama-4-scout-17b-16e-instruct` in the production fallback chain.** Updated in `litellm_config.yaml` in the same PR (search for the `equity-agent/fallback-llama4scout` block).

Four reasons it's the obvious call:

1. **Fixes the QNT-128 bug** — Qwen3-32B is unsafe in production: 12/16 hallucination_ok means a quarter of theses contain invented numbers. Removing it from the chain is the actionable retro outcome.
2. **Same provider, same key, no new failure mode** — both Qwen and Scout are Groq-hosted, so the LiteLLM client path is unchanged. Only the underlying model swaps.
3. **5× the TPD headroom over the default** — Llama-3.3-70B is 100K TPD; Scout is 500K TPD. So the fallback can absorb 5× the daily volume the default can, which matches the original capacity-rationale Qwen was promoted under.
4. **Strictly better quality than Qwen on every measure** — 16/16 vs 12/16 hallucination, judge 4.94 vs 1.44, cosine 0.342 vs 0.296. And materially faster (836 ms p50 vs 25 635 ms — 30× difference in wall time, mostly because Scout doesn't waste tokens on a `<think>` scratchpad).

**Default decision: keep Llama-3.3-70B (QNT-138 closed-out).** The QNT-129 ship deferred the Scout-as-default question because the reference sweep was TPD-truncated at record 10/16, leaving a 9-clean-records preview where Scout led cosine by +0.006. [QNT-138](https://linear.app/noahwins/issue/QNT-138) re-ran the reference on a fresh 100K TPD bucket and the deferred question is now answerable on a full row. Pre-registered decision rule (set before the re-run, not after): *Scout cosine lead ≥ 0.02 OR judge lead ≥ 2.0 → promote Scout; else keep Llama.*

Measured on the clean reference row:

- Scout cosine lead: **−0.022** (Llama leads 0.364 vs Scout 0.342)
- Scout judge lead: **+0.25** (4.94 vs 4.69)

Both metrics fall below the promotion threshold, and cosine has flipped sign once a complete sweep is on the table. Llama remains default; Scout remains fallback (5× the TPD ceiling and strictly better than the Qwen it replaced — that QNT-129 conclusion is unchanged).

The remaining Scout edge is per-call latency (169 ms vs 311 ms p50, ~2×). Wall-clock p50 (836 ms vs 8 398 ms) makes the gap look 10× larger but is mostly the per-alias bench TPM cap queueing late-record plan calls on Llama; the production default has no such cap. A 2× per-call latency edge alone doesn't warrant disrupting a default with months of production track record.

`litellm_config.yaml` is intentionally unchanged by QNT-138 — the chain shipped in QNT-129 (`equity-agent/default` → `equity-agent/fallback-llama4scout`) is the chain the bench data supports.

---

## Revisit cadence

Re-run this bench:

- **Quarterly** (next: 2026-07-26) as a calendar trigger — model availability and free-tier policy on Groq + Google AI Studio churn faster than that on the no-card free tier.
- **When a candidate model graduates from preview** — `gemini-3.1-flash-lite-preview` is the obvious one; it could either go GA at a different price point or get withdrawn.
- **When Groq deprecates one of the candidates** — Groq has rotated models in the past (the QNT-128 ADR-011 revisit-trigger calls this out explicitly).
- **When a regression shows up in production** — `evals/history.csv` is the first place to look; if the production model's row has drifted, re-run this bench against the current candidates before swapping.
- **When a new free-tier model lands** — add an alias under `# QNT-129 bench aliases` in `litellm_config.yaml`, run that one sweep, and append the row here without re-running the others (the run_id suffix encoding makes per-model aggregates additive).

To re-run a single candidate (e.g. after Groq updates `qwen3-32b`):

```bash
make dev-litellm  # if not already up
uv run python -m agent.evals --model equity-agent/bench-qwen3-32b
# then check the new tail rows in evals/history.csv and update the table above
```

To re-run the full bench, the harness script lives at `/tmp/qnt129-bench.sh` (copy into `scripts/` if it should become a permanent harness). The post-hoc summarisers were promoted to the repo by QNT-138: `scripts/evals/aggregate_bench.py` (history.csv reader, defaults to latest sweep per alias — pass `--all-sweeps` for legacy behaviour) and `scripts/evals/token_summary.py` (Langfuse metrics reader; needs `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` from `.env`, run with `uv run --env-file .env`).

---

## Follow-ups (filed separately)

- **Capture synthesize tokens** — `traced_invoke` drops `model_name`/`usage_details` for `with_structured_output()` responses because they aren't `AIMessage`. Switch graph.py to `with_structured_output(Thesis, include_raw=True)` and read tokens off `response['raw']`. Affects every Langfuse trace today, not just bench.
- **Reference theses for the four-section schema** — `goldens/questions.yaml` references are flowing prose; the post-QNT-133 thesis output is structured into Setup / Bull / Bear / Verdict. Cosine vs prose underestimates structural matches; rewrite references in the structured shape so cosine reads cleanly.
- **README link from QNT-66** — link this doc from the architecture section of the README ([QNT-66](https://linear.app/noahwins/issue/QNT-66)) once that ticket starts.
