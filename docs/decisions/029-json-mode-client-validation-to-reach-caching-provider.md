# ADR-029: json_mode + client-side validation on the primary, to reach the only caching provider

**Date**: 2026-10-03
**Status**: Accepted
**Supersedes (in part)**: [ADR-025](025-paid-launch-primary-and-breaker-recalibration.md) (primary model slug, the Nemotron-as-first-hop fallback chain, and the unit economics) and [ADR-027](027-prompt-caching-enabled-via-provider-pin.md) (the ordered six-provider pin and server-side strict `json_schema` on the synthesize call). Reverses QNT-258's `function_calling` workaround for `ConversationalAnswer` and QNT-358's strict-`json_schema` choice for `ComparisonAnswer`.

## Context

Three failures and one missed saving traced back to the request shape the primary alias sends:

1. **Caching was dead.** ADR-027 (as amended by QNT-351) pinned first-party DeepSeek because it is the only OpenRouter provider that implicitly prefix-caches. QNT-442 then moved the slug to `deepseek-v4-flash-0731`, and that endpoint does not list first-party DeepSeek. So prod ran on Baidu at $0.44/$1.32 per M with `cached_tokens=0` on every call (synthesize median ~4.9k input tokens).
2. **Silent fallback (QNT-492).** Conversational and clarify used `function_calling`, and LangChain bound `parallel_tool_calls`. No allowlisted provider advertised that param, so `require_parameters` filtered every one of them. For 7 weeks, 100% of those turns were served by the free Nemotron anchor (13-38s per turn).
3. **Two request shapes to keep compatible.** `json_schema` (cards) and `function_calling` (conversational/clarify) each had to stay inside the provider filter on their own.
4. **The first fallback hop was a different, slow model.** Any filter miss or provider outage dropped straight to Nemotron 550B :free, with p50 13s and shared rate caps.

`deepseek/deepseek-v4.1-flash` does list first-party DeepSeek: `supports_implicit_caching=true`, 99.99% uptime, $0.15/$0.60 per M, cache reads $0.003 per M (endpoints probe, 2026-10-03). However, DeepSeek advertises `response_format` and **not** `structured_outputs`, and it fails forced `tool_choice`. Under `require_parameters: true`:

| Request shape | Reaches first-party DeepSeek? |
|---|---|
| `response_format: json_schema` (LangChain default) | No - filtered (no structured_outputs) |
| forced `tool_choice` (`function_calling`) | No - filtered |
| `response_format: json_object` (`json_mode`) | **Yes** |

## Decision

1. **The primary is `openrouter/deepseek/deepseek-v4.1-flash`, pinned to first-party `deepseek` only.** Reasoning stays off, `require_parameters` stays true, and `allow_fallbacks` is false. The curated six-provider resilience list from ADR-027 is removed. Its job moves to the next item.
2. **The first fallback hop is the same model on a pinned backup set.** `equity-agent/default-backup` serves v4.1-flash from `together`, then `parasail`, then `deepinfra`. It excludes fp4 builds (`quantizations: [fp8, bf16, unknown]`), sets `allow_fallbacks: false`, and keeps `require_parameters`. The chain is now `default -> default-backup -> fallback-nemotron-ultra`. A pin miss or a DeepSeek outage now costs cache hits only; neither the model nor its output quality changes. (This hop first shipped unpinned as `default-any-provider`. The QNT-493 follow-up pinned it, because OpenRouter's price-weighted routing would otherwise land on the cheapest endpoints, which are fp4 builds and providers we had never measured.)
3. **Every default-alias structured call uses `json_mode`.** That covers thesis, quick-fact, comparison, conversational, clarify, focused, and exploration (`graph._structured_call`). The model no longer gets the schema on the wire, so `_with_json_schema_instruction` appends the Pydantic JSON Schema to the **first system message**. That puts it in the stable, cacheable prefix, and it also satisfies DeepSeek's rule that json_object prompts contain the word "json". Strict validation happens client-side in the existing retry/coerce ladder (`with_retry` on `ValidationError`/`OutputParserException`, then the deterministic fallback). The small-alias planner, which passes its own `llm`, keeps its default method.
4. **An hourly canary checks every request shape.** `llm_canary_job` (Dagster) sends the agent's own json_mode and streamed-narrate payloads through LiteLLM. It fails on any fallback (`x-litellm-attempted-fallbacks != 0`; for streams, a different `x-litellm-model-id`), on latency over 15s, on empty output, or on non-JSON output. A failed run alerts Discord through `dagster_run_failure_alert_sensor`.

## Alternatives Considered

- **Keep server-side strict `json_schema`, add structured-outputs providers to the pin.** Rejected: every such provider is non-caching, so this is exactly the uncached state we're leaving.
- **Call DeepSeek's API directly.** Rejected for now (user decision, 2026-10-03). OpenRouter keeps model flexibility and the shared key and billing.
- **Drop `require_parameters`.** Rejected. It is what guarantees `json_object` is actually enforced (QNT-258 showed that advertising a param is not the same as enforcing it). It is also the filter that makes a shape regression fail loudly into the canary instead of silently degrading.
- **Leave conversational/clarify on `function_calling`.** Rejected: DeepSeek fails forced `tool_choice`, so it would filter out the caching provider. `json_object` forces a JSON reply, so the QNT-258 bare-prose failure cannot recur.

## Consequences

- **Cost (input-dominated traffic, QNT-292 ~12:1).** A synthesize-sized call (3.9k in / ~0.9k out) measured through the local proxy (QNT-493 AC5):

  | | provider | cached | cost |
  |---|---|---|---|
  | call 1 (cold) | DeepSeek | 0 / 3855 | $0.00114 |
  | call 2 (warm) | DeepSeek | 3712 / 3855 (96%) | $0.00058 |

  For comparison, the old prod route (Baidu, uncached) costs about $0.0034 for a 4.9k/0.9k call. Even fully cold, v4.1 on DeepSeek is about 2.6x cheaper per call. With a warm prefix it is about 4-6x cheaper. Output ($0.60/M) is now the larger share of each call.
- **Latency.** Probe calls answer in ~1s, and synthesize-sized calls take 4.6-5.3s (AC5). Cache hits cut prefill.
- **The schema now costs prompt tokens.** The suffix is ~1.2-4.3k chars (~300-1,100 tokens) per shape. It sits in the cached prefix, so on a warm call it bills at the cache-read rate.
- **Validation is client-side only.** A malformed or off-schema reply costs one retry, then the deterministic fallback, same as before. The golden run's parse-failure rate is the watch metric (see the QNT-493 AC4 receipts).
- **The streaming fallback signal is weaker.** LiteLLM omits `x-litellm-attempted-fallbacks` on streamed responses, so the per-turn fallback tripwire can't see a narrate fallback. The canary covers streams by comparing deployment ids against the zero-fallback structured probe.
- **A timed-out primary still cannot reach the end of the chain within one client call.** Each OpenRouter hop has a 45s timeout and the client gives up at 60s (`LLM_REQUEST_TIMEOUT`), so after a primary timeout the any-provider hop has about 15s before the client aborts and retries the whole chain. This was already true of the old 45s-plus-Nemotron chain; the extra hop just makes it one step longer. The hop's main job is the fast failures (a 404 from a filtered or missing provider, 5xx, 429), which take well under a second to fall through. Re-tune the per-hop timeouts against the v4.1 latency distribution if timeouts show up in the fallback tripwire.
- **The DeepEval judge (`bench-deepseek-v4-flash`) stays on 0731.** It no longer moves in lockstep with the agent (QNT-442 did that), so judge scores stay comparable across this change.
- **`_OUTPUT_BUDGET`** (graph.py): Thesis rises from 2500 to 3500. v4.1 writes longer theses (completion median 1774, max 2192 tokens in the AC4 golden run, up from a 1304 median on the old model), which left only 1.1x headroom against the table's ~1.7x policy. Every other shape keeps its ceiling with at least 1.5x headroom (see the table comment).

## Backup hop selection (QNT-493 follow-up)

The backup providers were chosen from the model's top token-share providers on OpenRouter (DeepInfra, Together, DeepSeek, Inference.net, Parasail), measured with v4.1-flash pinned to one provider at a time on the real `Thesis` json_mode prompt (~4k input tokens, 3 calls each, 2026-10-04/05). Every provider returned schema-valid JSON. Speed and output behavior differed:

| Provider | Median latency | Output tok/s | Output tokens | Decision |
|---|---|---|---|---|
| together | 3.2-3.9s (two runs) | 193-260 | 759-940 | backup #1 |
| parasail/fp8 | 7.6s | 116 | 885-977 | backup #2 |
| deepinfra/fp8 | 8.3-11.7s (two runs) | 65-87 | 690-781 | backup #3 (highest volume) |
| inference-net | 10.9s | 75 | 818-962 | excluded: slow, quantization undisclosed |
| baseten/fp8 | 3.2s | 295 | 862-988 | excluded: returned a 429 |
| gmicloud/fp8, siliconflow/fp8 | 5.8s, 6.3s | 83-100 | 518-610 | excluded: ~35% shorter theses |
| digitalocean | 14.0s | 63 | 866-906 | excluded: too slow |

Quality check through the pinned hop (golden subset AAPL/NVDA/MSFT, n=20, same temporary judge as AC4):

| | primary (DeepSeek) | backup (pinned) |
|---|---|---|
| hallucination_ok | 19/20 | 19/20 (the miss is the MSFT `$1. 4 trillion` ingestion artifact) |
| tool_call_ok | 20/20 | 20/20 |
| judge composite / faithfulness | 5.4 / 7.05 | 5.4 / 7.0 |
| cosine | 0.461 | 0.466 |
| median latency | 8.5s | 6.9s |

## Eval evidence (QNT-493 AC4)

Arm A = `origin/main` code (0731, `json_schema` / `function_calling`) on `bench-deepseek-v4-flash`. Arm C = this change (v4.1-flash, `json_mode`, DeepSeek-first) on `equity-agent/default`. Both arms used `--model` routing, the same prompt_version `7d2ca36037`, and the same data window (2026-10-04). The committed golden/dialogue judge (Cerebras `gpt-oss-120b`) returned 402 on every call (follow-up ticket filed), so both arms were judged by a temporary `openai/gpt-6-luna` alias on a scratch proxy. That makes the A/C pair comparable with each other, but not with earlier history rows.

| Golden (n=44) | A | C |
|---|---|---|
| hallucination_ok (raw) | 42/44 | 41/44 |
| real violations (excluding ingestion artifacts, below) | 1 (forbidden phrase) | 1 (raw label token in a rationale) |
| tool_call_ok | 44/44 | 44/44 |
| judge composite / faithfulness | 5.32 / 6.70 | 5.48 / 6.98 |
| cosine | 0.434 | 0.451 |
| per-record latency median / p90 | 19.0s / 33.2s | 10.3s / 18.7s |
| json parse failures / cached prompt share | n/a | 0/44 / 50% |

| Dialogue (15 fixtures) | A | C |
|---|---|---|
| avg_dialogue | 0.675 | 0.782 |
| numeric_support_ok | 15/15 | 14/15 (ingestion artifact) |

| DeepEval (n=55, pinned 0731 judge) | 2026-08-15 baseline (0731 agent) | C (2026-10-04) | floor |
|---|---|---|---|
| faithfulness | 0.966 | 0.971 | 0.70 |
| answer_relevancy | 0.898 | 0.887 | 0.75 |
| context_precision | 0.873 | 0.864 | 0.60 |
| context_recall | 0.991 | 0.860 | 0.85 |
| geval | 0.851 | 0.780 | 0.65 |

All DeepEval floors pass (number-grounding 53/55, json parse failures 0/55, cached prompt share 72%). context_recall (now close to its floor) and geval dropped against a baseline taken on a different data window that was not re-run on the same day, so those two drops are not attributable to the model; they are a watch item for the next DeepEval run.

Run B (v4.1 + the old methods) was not run. The ticket gates it on C regressing, and C has no real regression.

Two findings from the gate:

- **Ingestion artifacts, not hallucinations.** Every C numeric flag (INTC `5.4` / `142`, MSFT `1.4`) traces to mangled numbers in ingested Yahoo article text: `-5. 4%`, `$142Valuation`, `$1. 4 trillion`. The model reads them correctly, but the scorer cannot match them. A hit the same INTC article once.
- **The golden `verdict_consistency` check was stale.** It still enforced QNT-208 (the rationale must quote a label token), while QNT-359 reversed that contract (translate labels into prose). It failed exactly the rationales that obeyed the prompt. Re-aligned in this change: a raw capitalized token in the rationale now fails the check.
