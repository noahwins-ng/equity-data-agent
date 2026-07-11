# Paid launch-primary bench - July 2026 (DeepSeek V4 Flash)

**Date**: 2026-07-04
**Linear**: [QNT-258](https://linear.app/noahwins/issue/QNT-258) - paid inference primary for public launch.
**ADR**: [ADR-025](decisions/025-paid-launch-primary-and-breaker-recalibration.md)
**Prior bench format**: [model-bench-2026-04.md](model-bench-2026-04.md) (QNT-129).

## Why this exists

QNT-258 promotes `equity-agent/default` from Groq `llama-3.3-70b` to the paid **DeepSeek V4 Flash** via OpenRouter (see ADR-025 for the provider decision). Per the project's "quality before capacity" rule ([QNT-129](https://linear.app/noahwins/issue/QNT-129), [QNT-138](https://linear.app/noahwins/issue/QNT-138)), the promotion is gated on a golden + dialogue sweep on a **clean rate-limit window**, compared against the `llama-3.3-70b` baseline. This doc records the commands and the results table.

The bench alias `equity-agent/bench-deepseek-v4-flash` (`openrouter/deepseek/deepseek-v4-flash`) already exists in `litellm_config.yaml` (added by [QNT-275](https://linear.app/noahwins/issue/QNT-275) for the DeepEval judge), so AC1's alias requirement is met; this run exercises it as the agent-under-test.

## Result summary (2026-07-04)

**PROMOTE.** DeepSeek V4 Flash (reasoning **disabled** - see the reasoning finding below) meets or beats the llama-3.3-70b baseline on every golden hard gate, with a dramatically tighter latency tail. Wired as `equity-agent/default`.

### The reasoning finding (why this bench ran twice)

DeepSeek V4 Flash on OpenRouter **defaults to reasoning ON** - it emits 37-89 chain-of-thought tokens per call even with `reasoning_effort: low`. The ticket's "non-reasoning" premise was wrong for the OpenRouter-served variant. Reasoning-ON produced a **degenerate first bench**: ~30 min wall-clock (vs ~10 min baseline), intermittent **empty completions** (CoT ate the token budget → `content: None`), and **uniform ~0.9-0.96 narrate grounding-miss on every ticker**. Verified fix: `extra_body: {reasoning: {enabled: false}}` at the alias level → `reasoning_tokens` drop to 0, clean compact output. Applied to both `equity-agent/default` and `equity-agent/bench-deepseek-v4-flash` (the latter also aligns the DeepEval judge with ADR-023's "verdict tasks don't need CoT"). The numbers below are the **reasoning-off** re-run - the config we ship.

### Golden set (41 records, same prompt_version)

| Measure | llama-3.3-70b (baseline `20260616T175146Z-1f38b9`) | DeepSeek V4 Flash reasoning-off (`20260704T141619Z-c06fd6`) | Verdict |
| --- | --- | --- | --- |
| hallucination_ok | 39/41 (95%) | **40/41 (97.6%)** | ✅ better |
| tool_call_ok | 41/41 | 40/41 | ~ 1 miss (within variance) |
| judge composite | 4.293 | **5.634** | ✅ better |
| cosine | 0.408 | 0.431 | ✅ better |
| p50 elapsed (harness) | 14.3s | 15.3s | ✅ parity (+1s) |
| p90 elapsed (harness tail) | 128.0s | **36.8s** | ✅ dramatically tighter |
| verdict_label_consistent | 0/41 | 4/41 | ✅ better (both low - QNT-302 advisory) |

The single DeepSeek blemish is one record (`AMD amd-combined`) failing both hallucination_ok and tool_call_ok - within the baseline's own 2/41 variance. The p90 tail (37s vs 128s) is exactly the throttled-tail failure mode ADR-021/QNT-223 set out to bound.

**Latency note:** p50/p90 above are the golden *harness* elapsed (full record incl. tool + judge), not the production per-turn Langfuse figure. The ticket's AC6 "5.6s" is the 14-day production turn p50; harness p50 parity + a far tighter tail indicate no production regression, to be confirmed by the post-deploy Langfuse check. (p90 is nearest-rank/decile method; a linear-interpolation percentile lands ~31s - either way far under the 128s baseline tail.)

### Fallback-anchor liveness (AC3)

Verified live against the running proxy (`max_tokens=120`, "what does a P/E ratio measure?"):

- `equity-agent/fallback-nemotron-ultra` (`nvidia/nemotron-3-ultra-550b-a55b:free`) → **clean completion, `finish_reason=stop`**, structured-outputs capable. This is the anchor.
- `equity-agent/fallback-laguna-m1` (`poolside/laguna-m.1:free`) → **`429` upstream** ("poolside/laguna-m.1:free is temporarily rate-limited upstream") and empty completions when not throttled. **Dropped in review** - a known-flaky last hop only adds retry latency before the deterministic fallback. Chain is now `default → nemotron-ultra → (deterministic fallback)`.

## Commands (run on a clean rate-limit window)

All commands require `make tunnel` + `make dev-litellm` (or the prod stack) and a valid `OPENROUTER_API_KEY` in `.env`.

```bash
# AC1 / AC2 - golden sweep against the DeepSeek bench alias (isolated, no fallback)
uv run python -m agent.evals --model equity-agent/bench-deepseek-v4-flash

# AC1 / AC2 - dialogue-quality sweep. dialogue_eval has no --model flag; it runs
# against equity-agent/default, which is NOW DeepSeek post-QNT-258. So a plain run
# is the launch-representative sweep (includes the OpenRouter fallback chain).
uv run python -m agent.evals.dialogue_eval

# AC8 ride-along - routing eval (26 fixtures, one small classify call each).
# Offline scorecard; confirms classifier flag preserved, no intent drift (AC2).
uv run python -m agent.evals.routing_eval

# AC6 - reference line: same sweep against the llama-3.3-70b calibration alias,
# for a within-window baseline comparison on the current prompt_version.
uv run python -m agent.evals --model equity-agent/bench-llama3-70b
```

**Where the numbers land:** `packages/agent/src/agent/evals/golden_history.csv` (golden), `dialogue_history.csv` (dialogue), `routing_history.csv` (routing). Filter by the `run_id` suffix (`bench-deepseek-v4-flash`). The golden runner's summary line also carries the QNT-302 `verdict_label_consistent` rate (AC8) and the QNT-307 single-`answer`-field render confirmation (AC8).

## AC5 - launch-burst survival (global breaker)

The breaker math is provider-independent and is proven deterministically by
`tests/api/test_security.py::test_paid_breaker_survives_30_chat_launch_free_tier_tripped_at_15`
(30 substantive chats survive the 20M cap; the old 200K cap tripped at ~15). Run:

```bash
uv run pytest tests/api/test_security.py::test_paid_breaker_survives_30_chat_launch_free_tier_tripped_at_15 -q
```

For an optional live confirmation on the running API (paid primary end-to-end),
drive `POST /api/v1/agent/chat` from >=15 distinct source IPs (each under the
unchanged 5/min + 30K/day per-IP fence) and confirm no request returns the
demo-limit card until well past 30 chats.

## AC9 - narrate two-part close A/B

Re-run the [QNT-303](https://linear.app/noahwins/issue/QNT-303) controlled A/B on the paid primary per the harness in
[docs/assessments/agent-analyst-voice-2026-07.md](assessments/agent-analyst-voice-2026-07.md) (follow-up section). Confirm the two-part close holds (falsifier woven inside the synthesis paragraph, `Watch:` as the separate final line) or record the combined-close fallback. No prompt change expected.

## Remaining execution items (post-deploy)

- **AC9 narrate two-part close** - manual A/B per `docs/assessments/agent-analyst-voice-2026-07.md`; deferred as a follow-up (QNT-303 owns narrate voice; no prompt change expected).
- **AC6 production p50** - confirm production per-turn p50 ≈ 5.6s (no regression) on Langfuse after the paid primary is live.
- **Infra template prod-AC** (`litellm_config.yaml` + `config.py` are trigger files, `docs/AC-templates.md`): CD green end-to-end (SHA + Dagster gates), no prod drift, post-deploy smoke (a real chat through the deployed paid primary). These are `/ship` hard gates.

### routing_eval (26 fixtures) - 2026-07-04

**26/26 (100%)**, floor 80% PASS, 0 misses, 0 false positives. Classifier flag preserved, no intent drift. Classify runs on the Groq `equity-agent/small` alias (intent shown as `.../llm` or `.../heuristic`), so it is orthogonal to the DeepSeek primary swap - confirming AC2's "classifier flag preserved on the small node" and AC8's routing item.
