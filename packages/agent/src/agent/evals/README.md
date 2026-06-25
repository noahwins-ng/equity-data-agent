# Agent Evaluation Harness (QNT-67)

Evaluation framework for the LangGraph agent. Lives in-tree under `packages/agent/src/agent/evals/` so evals run against the exact code that ships, against a locally-running CLI (`uv run python -m agent analyze NVDA`).

**Design intent**: this harness is the single most important piece of AI-Engineering signal in the repo. It operationalises the ADR-003 contract ("the LLM never calculates") and provides a measurable quality signal for the prompt across versions. Reusable enough to extract as a standalone repo later.

## Four eval types — all required, not optional

### (a) Numeric-claim hallucination detector — `hallucination.py`

For every thesis the agent produces, regex every numeric claim out of the text and assert each number appears in the tool-output report strings the agent received.

- "Verbatim" with a tight canonicalisation: leading `$`, trailing `%`, and comma thousand-separators are stripped (formatting, not arithmetic). Decimal precision is preserved, so a thesis writing `12.30` against a report saying `12.3` IS flagged — rounding is arithmetic.
- Any mismatch = test failure.
- Direct operational enforcement of ADR-003. If the LLM adds a number that wasn't in a report, it hallucinated — by definition.

### (b) Golden-set regression — `golden_set.py` + `goldens/questions.yaml`

16 curated `(ticker, question, reference_thesis, expected_tools)` records. Coverage invariant: at least one question per ticker in `shared.tickers.TICKERS` — enforced by `tests/agent/evals/test_questions_yaml.py`.

Per run, for each record:
1. Invoke `build_graph(...).invoke(...)` in-process with recording-wrapped tools (so the tool-call eval can see what was actually called).
2. Score the generated thesis against the reference thesis via:
   - **LLM-as-judge** per-axis scores (0–10 each) using the agent's own LLM via the LiteLLM proxy at `temperature=0.0`. Four axes: `faithfulness`, `structure`, `correctness`, `analyst_logic`. A `composite` column holds the rounded average of all four.
   - **Cosine similarity** over normalised term-frequency vectors. The original spec called for `all-MiniLM-L6-v2` embeddings; we ship the lighter zero-dep equivalent (same operation in a different vector space) to keep the harness portable. Swap `similarity.cosine` for an embedding-backed implementation behind the same signature when MiniLM is on the path.
3. Append one row per record to `history.csv`:
   `run_id, git_sha, prompt_version, ticker, question_id, question, faithfulness, structure, correctness, analyst_logic, composite, cosine, tool_call_ok, hallucination_ok, elapsed_ms`.
4. `history.csv` is committed so prompt-version quality is visible in `git log -p packages/agent/src/agent/evals/history.csv`.

### (c) Tool-call correctness — `tool_calls.py`

For each record, assert every tool in `expected_tools` was actually called. Over-fetching is allowed (the planner is told to over-fetch when in doubt) — only under-fetching fails.

### (d) Dialogue-quality judge — `dialogue_eval.py` + `goldens/dialogue.yaml`

12+ hand-written multi-turn fixtures replay the agent through the same
in-process graph path as the structured goldens. The judge is deliberately a
different LiteLLM alias from the agent under test:
`equity-agent/bench-cerebras-gptoss120b` (`cerebras/gpt-oss-120b`) scores the
production default (`groq/llama-3.3-70b-versatile`). Python still owns the
objective numeric-support check for the narrative bubble; the judge scores the
subjective dialogue axes: `analyst_likeness`, `helpfulness`,
`non_hallucination`, `exploration_quality`, and `voice_match`.

Dialogue rows append to the same `history.csv` with `eval_type=dialogue` and
blank structured-golden columns, preserving one reviewable quality ledger. Each
run also appends one `eval_type=dialogue_summary` row carrying the per-axis mean
(axis columns) plus its standard error (`*_se` columns) and the fixture count
(`dialogue_n`) — see "Making the dialogue eval trustworthy" below.

> **Superseded baseline (temp=0.2 era).** The QNT-214 baseline below was
> captured with the agent-under-test at `temperature=0.2`. QNT-218 pins the
> agent to `temperature=0` during the eval, so these numbers are **not
> comparable** to any temp=0 run and must not be used as the QNT-215 reference.
> The QNT-215 `+0.10 / +0.15` lift thresholds were calibrated off this stale
> baseline and must be re-derived against the temp=0 baseline before use.

QNT-214 baseline (temp=0.2, superseded), captured after QNT-216 history landed:

| Field | Value |
|---|---:|
| Run id | `20260530T055035Z-2b8838-dialogue` |
| Dialogues | 12 |
| Numeric support | 11/12 clean |
| Composite | 0.774 |
| Analyst-likeness | 0.779 |
| Helpfulness | 0.800 |
| Non-hallucination | 0.879 |
| Exploration quality | 0.600 |
| Voice match | 0.812 |

### Making the dialogue eval trustworthy (QNT-218)

The harness is a measurement instrument; QNT-218 hardens it so a single sweep
carries its own uncertainty, rather than averaging noise away with repeated
sweeps (which would drain the Groq budget).

- **Determinism.** The agent-under-test is pinned to `temperature=0` for the
  duration of each fixture (`set_temperature_override`, reset in a `finally`).
  The judge is already temp=0. This removes *sampling* variance only — Groq's
  MoE serving is still non-deterministic, which is exactly why the per-axis
  error bars below still matter. (It is therefore wrong to call a temp=0 run
  "deterministic".)
- **Self-aware single run.** Each axis mean is an average over the 12 fixtures,
  so one run reports its own dispersion band: `SE = sd_fixtures / sqrt(n)`,
  persisted on the `dialogue_summary` row and printed by `summarise`. This is a
  **descriptive scatter** of one sweep, not a lift test.
- **The QNT-215 gate is a paired per-fixture test, not two independent means.**
  The fixtures are shared between baseline and candidate, so the gate
  (`paired_delta_gate`) pairs them: `delta_i = candidate_i - baseline_i`,
  `SE_delta = sd(delta_i) / sqrt(n)`. A **lift** axis (`analyst_likeness`,
  `exploration_quality`) passes when `mean_delta > k * SE_delta` (`k=2`); every
  other axis is a **guardrail** (`non_hallucination`, `helpfulness`,
  `voice_match` — QNT-215's "no regression elsewhere") that passes when it does
  not significantly regress (`mean_delta >= -k * SE_delta`). The two tuples
  partition `DIALOGUE_AXES` so no axis goes silently unchecked. Pairing cancels
  the shared fixture-difficulty term an independent two-sample SE would
  double-count, so it is both tighter and conceptually correct. Note the gated
  lift lives on the *noisy* axes QNT-215's topology is trying to move —
  `non_hallucination` is a must-not-regress guardrail, never the gate metric.
- **Replication policy.** A full sweep replicated `n=2-3` times is reserved for
  the single final QNT-215 go/no-go decision, run **once on a verified clean
  rate-limit window** — never as routine iteration cadence. Directional
  iteration uses a single run on the targeted fixture(s). Routine multi-sweep
  averaging was explicitly rejected: it spends the scarce Groq budget on every
  iteration to average out a variance source temp-pinning removes for free.
- **Clean-window guards.** `precheck_environment()` fails fast (before any token
  is spent) if the LiteLLM proxy or report API is unreachable, so a sweep can
  never silently run on empty reports. `contamination_warning()` flags a run
  whose median fixture latency clears `CONTAMINATION_LATENCY_MS` (≈throttling)
  or that dropped any judge call, so a contaminated aggregate is never trusted.

### (e) RAG news-search eval — `news_search_eval.py` + `goldens/news_search.yaml`

Coverage for the QNT-222/225/226 semantic-news-search arc (RAG over the Qdrant
`equity_news` collection), which shipped with eval coverage only for the
*plumbing* (mocked-LLM unit tests for flag propagation, hit folding, provenance
parsing). This axis measures the two things those tests can't: whether the
search *fires* on the right asks, and whether retrieval returns *relevant*
headlines. The stakes are higher than a normal coverage gap — on the
targeted-news path the focused news card is dropped (QNT-226 narrative-only
shape), so a wrong firing decision or a retrieval miss degrades the entire
answer with no regression tripwire.

The fixture set (`goldens/news_search.yaml`) is 13 positive targeted-event asks
(litigation / CEO statement / buyback / recall / antitrust / partnership /
acquisition / layoffs / probe / merger phrasings, one per covered ticker) plus
6 negatives (generic "what's the news on X?", "how's sentiment?", price, thesis,
and an off-domain ask) that must NOT fire the search.

**Flag layer (hard gate on one direction).** `classify_intent_with_source(question)`
is run live; its `needs_news_search` must equal the fixture's
`expected_news_search`. The **only gated direction is a false positive** — a
generic ask wrongly firing RAG drops the focused card (QNT-226), so it's the
dangerous failure. Positive misses are reported as known-misses, not gated.

> **The flag is deterministic.** `classify_intent_with_source` returns
> `_is_targeted_news(question)` regardless of the heuristic/LLM path — QNT-229
> moved the firing boundary into code; the LLM's
> `IntentDecision.needs_news_search` field is guidance/back-compat only. So
> "flag accuracy" is a **keyword-routing contract**, not a model-judgment
> measurement, and it does not drift with the model. The live run still calls
> the real public entrypoint (the LLM fires for the *intent label* on
> heuristic-abstain cases), so it's a genuine live-classifier run and would
> catch a regression if the flag were ever re-coupled to the LLM. The offline
> half of this contract is pinned in
> `tests/agent/evals/test_news_search_yaml.py` (runs in the default unit sweep).

**Retrieval layer (report-only).** `search_news(ticker, question)` is run live
against Qdrant; a hit "matches" when any of the fixture's `expected_terms` is a
case-insensitive substring of the hit's headline or body.

> **Rolling-window assertion strategy (the ticket's main design question).** The
> 7-day news window rolls, so a frozen-headline assertion would go stale daily.
> We assert **structural relevance** — a term match against the live corpus —
> never a specific headline string. Retrieval is **reported, not gated**: a miss
> can mean a genuine recall gap OR simply that no such story is in this week's
> corpus (e.g. a litigation fixture returning only partnership headlines because
> that's what's in the window). Improving recall is a follow-up informed by these
> measurements (out of scope here, per the ticket).

**Clean-window publishing rule.** Like the other live evals, baseline numbers
are only published from a verified clean rate-limit window (Groq TPD for the
classifier calls, Qdrant quota for search). `contamination_warning()` flags a
run whose per-fixture flag latency clears one full `LLM_REQUEST_TIMEOUT` ceiling
(the Groq-throttle signature); when it fires, re-run before trusting the numbers.

Standalone, like the dialogue eval — needs the tunnel + live Qdrant + LiteLLM,
so it is **not** collected by pytest and does not run in the default unit sweep.
Exit codes: `0` = no false positives; `1` = a negative wrongly fired (or zero
fixtures); `2` = could not run a measurement (dev stack unreachable, or an
invalid `--only` id / fixture file) -- skipped gracefully, never read as a
false-positive failure.

QNT-231 baseline (sha `0e46407`, 2026-06-14, clean window):

| Layer | Result |
|---|---:|
| Flag accuracy | 19/19 (100%) |
| Negatives abstained | 6/6 |
| False positives | 0 |
| Positive misses | 0 |
| Retrieval hit-rate | 8/13 (62%) |

Retrieval misses at baseline (nvda-litigation, nvda-settlement, tsla-recall,
unh-investigation, v-merger) are rolling-window artefacts, not code bugs — the
corpus that week carried no matching legal/recall/merger story for those tickers
(tsla-recall returned 1 hit, unh-investigation 3; NVDA was dominated by the SK
partnership story). They are the input to a future recall-improvement ticket.

### (f) RAG retrieval eval — `retrieval_eval.py` + `goldens/retrieval.yaml` (QNT-261)

The news-search eval (e) scores *structural* relevance (does a hit contain an
expected term) and reports a rolling hit-rate; it cannot separate a retrieval
miss from a synthesis miss, and has no notion of *ranking* quality. This is the
industry-standard **stage-1 retrieval eval**: a labeled relevance set scored with
classic IR metrics (recall@k / MRR / nDCG via `ir_measures`), DETERMINISTIC and
LLM-free, kept **separate** from the generation judge. Build-before-upgrade — the
QNT-262 hybrid/rerank lift is measured against this baseline, not assumed.

Three files joined by query id (standard IR layout, `goldens/`):

| File | Role |
|------|------|
| `retrieval.yaml` | **topics** — 51 queries (news ×38 over all 10 tickers; earnings ×13 over NVDA+AAPL) + `anchor_terms` (the relevance criterion). Hand-authored. |
| `retrieval_qrels.trec` | **labels** — `qid 0 docid 1` per relevant Qdrant point id, captured from the live corpus by `--label`. |
| `retrieval_run.trec` | **frozen run** — the dense-retrieval ranking captured by `--baseline`; what the CI gate scores offline. |

**doc_id = the Qdrant point id** (UInt64) — what a vector search returns, aligned
across both corpora and the later S3-Vectors substrate (`equity_news` =
`blake2b(ticker:url_id)`, `equity_earnings` = `blake2b(ticker:doc_id:chunk_index)`).

**Labels are independent of the ranking under test.** A doc is relevant to a query
if its payload text contains one of the query's `anchor_terms`, scanned over the
*full* ticker-scoped corpus — not the dense top-k. That independence is what makes
recall/MRR/nDCG meaningful: we measure whether dense retrieval surfaces the
lexically-relevant docs against a ground truth it didn't produce. Labels + run are
frozen TREC files so the CI gate is reproducible even as the live corpus rolls;
`anchor_terms` document how the labels regenerate.

**2026-06-20 dense baseline** (current MiniLM-L6 dense retrieval, 51 queries):

| Metric | Value | Gate floor |
|--------|-------|-----------|
| recall@5 | 0.4802 | 0.40 |
| recall@20 | 0.7232 | 0.60 |
| MRR | 0.8450 | 0.70 |
| nDCG@10 | 0.7023 | 0.55 |

The low recall@5 (<half the relevant docs in top-5) is the headline finding and
the explicit motivation for QNT-262 (hybrid + rerank). The floors are regression
tripwires set ~0.08–0.15 below the measured baseline; re-derive them against a
fresh baseline whenever retrieval changes.

**The per-PR CI gate.** `tests/agent/evals/test_retrieval_eval.py` (marked
`eval`) loads the frozen qrels + run, recomputes the metrics via `ir_measures`,
and asserts each clears its floor — plus a number-grounding faithfulness tripwire.
No LLM keys, no network: free, fast, reproducible, blocking (`ci.yml` runs
`pytest -m eval` as its own step). The LLM-judged generation layer (DeepEval /
RAGAS, QNT-264) lives off the hot path (nightly / dispatch), never per-PR.

```bash
uv run python -m agent.evals.retrieval_eval --label      # rewrite qrels (live Qdrant)
uv run python -m agent.evals.retrieval_eval --baseline   # rewrite run + history (live Qdrant)
uv run python -m agent.evals.retrieval_eval              # offline score + gate (the CI path)
```

The baseline appends one `eval_type="retrieval"` row to `history.csv`
(`recall_at_5` / `recall_at_20` / `mrr` / `ndcg_at_10` / `retrieval_n`) stamped
with the same `git_sha` + `prompt_version` as every other eval type.

### (g) Multi-corpus routing eval — `routing_eval.py` + `goldens/routing.yaml` (QNT-263)

Eval (f) measures retrieval quality *within* a corpus; this measures whether a
question is routed to the *right* corpus in the first place — the senior
multi-corpus signal. For each fixture, `agent.intent.route_search_corpora(question)`
must equal the expected set of corpora (news and/or earnings, or neither). The
router is DETERMINISTIC and LLM-free (`_is_targeted_news` + `_is_earnings_search`),
so it runs **offline** in the default pytest sweep
(`tests/agent/evals/test_routing_yaml.py`) as well as standalone for the scorecard.

24 fixtures across four routing classes (news-only, earnings-only, both, neither);
coverage floors keep every class populated so the "both" signal can't silently
collapse. The deterministic gate fails on any misroute — a miss is a bug, not a
model wobble. **2026-06-20: 24/24 (100%).**

```bash
uv run python -m agent.evals.routing_eval                 # offline scorecard + gate
uv run python -m agent.evals.routing_eval --only nvda-ceo-guidance
```

### (h) LLM-judged generation eval — `deepeval_eval.py` + `test_deepeval.py` (QNT-264)

Evals (a)–(g) are deterministic or single-axis. This is the **LLM-judged nuance
layer** the 2026 two-stage RAG-eval blend calls for (docs/v2-overall-enhancement.md
"RAG eval framework", Track 2.7): the **RAGAS metric set** — faithfulness, answer
relevancy, context precision, context recall — plus one **custom G-Eval**
(`VerdictGroundedness`: is the investment verdict justified by the retrieved
evidence, without overstating confidence). All run through **DeepEval**, the
pytest-native production CI-gating framework that subsumes RAGAS (same metrics +
G-Eval, one framework — standalone RAGAS was dropped to avoid redundant tooling).

**Off the per-PR hot path (the whole point).** ci.yml wires no LLM keys and our
free-tier budget (Gemini 20 RPD, Groq TPD) makes judge-on-every-PR a bad fit, so
the suite is marked `deepeval` — neither the unit step (`-m "not integration and
not eval"`) nor the deterministic RAG gate (`-m eval`) collects it. It runs in a
**separate workflow** (`.github/workflows/llm-eval.yml`, nightly `schedule:` +
`workflow_dispatch`, stack/judge credentials as job-scoped secrets) and locally.
The per-PR RAG gate stays the deterministic one (eval (f), QNT-261).

**Judge routing + budget (AC2).** The judge is the SAME pinned free model the
dialogue judge uses (`equity-agent/bench-cerebras-gptoss120b` →
`cerebras/gpt-oss-120b`), reached through the LiteLLM proxy via `get_judge_llm()`
— no new provider key. A custom `LiteLLMJudge(DeepEvalBaseLLM)` wraps it;
`generate` honours DeepEval's optional `schema` kwarg via LangChain
`with_structured_output`. Gated to a **SAMPLE** (`DEEPEVAL_SAMPLE`, default 4
records) on a clean window: each record costs **~8–12 judge calls** across the
five metrics, so a 4-record run is **~32–48 calls** — inside the free tier. Metrics
run `async_mode=False` so the calls serialise rather than burst the rate limit.

**Coexistence, not replacement (AC4).** The in-house number-grounding check (eval
(a)) is retained and asserted additively per case — it's a stricter, deterministic,
verbatim faithfulness layer for financial figures than generic LLM-judged
faithfulness. DeepEval is the nuance layer on top.

**Recorded alongside the IR metrics (AC5).** Each run appends one
`eval_type="deepeval"` row to `history.csv` (`deepeval_faithfulness` /
`deepeval_answer_relevancy` / `deepeval_context_precision` /
`deepeval_context_recall` / `deepeval_geval` / `deepeval_n`), stamped with the same
`git_sha` + `prompt_version` as every other eval type. The `deepeval_*` prefix keeps
these 0–1 floats distinct from the integer golden-set `faithfulness` judge axis.

**Soft by default (the established judge philosophy).** Like the golden judge
(`EVAL_MIN_JUDGE` off until history earns a trustworthy number), the DeepEval
metric scores are a *recorded soft signal*. The pytest suite hard-asserts only
the real contracts — every RAGAS axis produced a score, and the deterministic
number-grounding ran additively (AC4). DeepEval's canonical `assert_test`
threshold gate is opt-in behind `DEEPEVAL_ENFORCE_THRESHOLDS`; enable it once a
clean ≥50-record baseline re-derives the floors, the same way the retrieval gate
floors are anchored to a measured baseline (not the design-doc aspirations).

The thresholds (faithfulness/context-precision/context-recall ≥ 0.8,
answer-relevancy ≥ 0.75, G-Eval ≥ 0.7) are the design-doc calibration targets.

**First recorded baseline** (run `20260621T042449Z-b07f37-deepeval`, sha
`b0d2ccb`, 2026-06-21, clean window, 4 focused-query records):

| Metric | Score | Design target |
|--------|------:|--------------:|
| faithfulness | 0.97 | ≥ 0.8 |
| answer_relevancy | 0.76 | ≥ 0.75 |
| context_precision | 0.88 | ≥ 0.8 |
| context_recall | **0.29** | ≥ 0.8 |
| VerdictGroundedness (G-Eval) | 0.93 | ≥ 0.7 |
| number-grounding (deterministic) | 4/4 clean | — |

**The low `context_recall` is a measurement-design finding, not a retrieval bug.**
Context recall checks whether the *reference answer's* statements are
reconstructable from the retrieval context. The golden references are hand-written
analyst *synthesis* (verdicts, interpretation), while the retrieval context is the
raw pre-computed report data — so the synthesized claims aren't directly
attributable to a context chunk and recall reads low. This is the same shape as
the structure axis scoring 0 on non-thesis query shapes (above): the framework
surfacing a real signal about the eval inputs, not a code defect. The follow-up is
a recall-appropriate golden set (references that mirror the retrieved slice) +
re-derived floors on a ≥50-record clean window, before flipping
`DEEPEVAL_ENFORCE_THRESHOLDS` on.

```bash
uv run python -m agent.evals.deepeval_eval                 # sample run + record (needs stack)
uv run python -m agent.evals.deepeval_eval --sample 8
uv run python -m agent.evals.deepeval_eval --only NVDA --no-record
uv run pytest -m deepeval -v                               # the pytest cases (live test skips w/o stack)
```

### (i) RAG impact eval — `rag_impact_eval.py` + `goldens/rag_impact.yaml` (QNT-277)

Evals (e)–(h) score retrieval (recall@k / MRR / nDCG) and generation (RAGAS /
G-Eval) **in isolation**. None catches the failure mode "search fired, retrieved a
relevant hit, and the answer ignored it" — the exact gap that let the synthesis
demotion (QNT-276) go unnoticed while every component eval stayed green. This is
the **end-to-end contribution** eval: does a retrieved-only fact actually REACH
the answer?

**Stub the tools, key on the answer text.** Each fixture compiles the graph with
`build_graph` dependency injection: the report tools return a canned digest that
OMITS the planted fact, and `search_news_tool` / `search_earnings_tool` return a
fixture hit carrying a **coined** proper noun (a name the model can't memorise or
paraphrase). The assertion is on the user-facing ANSWER TEXT — does the coined
entity appear? — not on `retrieved_sources` shape or fold rendering. That contract
survives the QNT-276 refactor, so the only way a fixture flips RED→GREEN is a
genuine behavior change. Because search is stubbed, the eval touches **neither
Qdrant NOR Cohere** — the only model calls are the agent's own classify +
synthesize/narrate on the Groq free tier. Runs off the per-PR hot path
(`workflow_dispatch` / local), like DeepEval, with zero rerank-quota cost. The
offline fixture validation (`tests/agent/evals/test_rag_impact_yaml.py`) DOES run
in the unit sweep.

**Eval-first ordering (Option A).** This ships FIRST as a RED baseline that proves
the gap on pre-QNT-276 behavior; QNT-276 then lands and flips it GREEN. A
`negative_control` (search returns `"[]"`) asserts the answer does NOT fabricate
the coined entity. A positive whose search never fired is reported as `misrouted`
(an intent-routing axis owned by evals (e)/(g)) and kept out of the pass-rate.

**Recorded alongside the other eval rows.** Each run appends one
`eval_type="rag_impact"` row to `history.csv` (`rag_impact_pass_rate` /
`rag_impact_n`), same `git_sha` + `prompt_version` stamping as every other type.

**First recorded baseline** (run `2441f9bf`, sha `f1d419c`, 2026-06-23, clean
window, 8 fixtures): `rag_impact_pass_rate` **0.875** (7/8 gated) — the one FAIL is
`msft-guidance-earnings`, where the retrieved earnings fact is dropped from the
answer (the earnings→fundamental synthesis path demotes it). That stable failure
is the evidence the gap is real; QNT-276 should flip it to 1.0.

```bash
uv run python -m agent.evals.rag_impact_eval                  # full run + record (needs LiteLLM)
uv run python -m agent.evals.rag_impact_eval --only nvda-antitrust-news
uv run python -m agent.evals.rag_impact_eval --no-history
```

**Trustworthiness (QNT-278).** `contamination_warning` now flags BOTH throttle
signatures: the slow one (a call ran to its timeout ceiling) and the FAST one (a
gated positive completed under `CONTAMINATION_FAST_LATENCY_MS` ~2.5s — Groq
returned a truncated completion that silently drops the planted entity, the
`msft-guidance-earnings` in-sweep flake). The msft fixture was reframed to
must-quote ("which named initiative did MSFT management tie its guidance to?") so
a short generation cannot answer without the coined entity, matching amzn's
robustness.

### (j) Live end-to-end RAG smoke — `rag_smoke_eval.py` + `goldens/rag_smoke.yaml` (QNT-278)

Evals (e)–(i) each score ONE layer: IR scores retrieval, DeepEval scores
generation, and rag_impact (i) **stubs** search so it tests fold→prompt→answer but
by construction cannot see retrieve→rerank. That seam is exactly where the QNT-276
demotion AND the QNT-279 boilerplate leak lived — every component eval green, the
end-to-end contribution wrong, the first real check a human in the prod UI. This
harness runs hand-picked queries through the **WHOLE chain against the real Qdrant
+ Cohere** (nothing stubbed) and asserts what the isolated evals cannot:

* **surfaced-source relevance** (the QNT-279 axis) against the per-corpus rerank
  floor it ASSERTS against (`RERANK_FLOORS`, mirroring `api/routers/search.py`).
  `relevant` fixtures (narrow asks) require the top hit to clear the floor;
  `boilerplate_guard` fixtures (broad asks) require NO surfaced hit below it — an
  empty result is the correct broad-ask outcome.
* **the retrieved fact reaches the answer** (the QNT-276 axis) — a distinctive
  term DERIVED from the live top hit (a coined figure / proper noun, never a
  generic word the canned report carries) must appear in the answer. Deriving it
  from the live hit keeps the assertion sound against the rolling news window.

On a **pre-QNT-279 build** the `boilerplate_guard` rows FAIL (sub-floor 8-K "About
&lt;co&gt;" surfaces); the floor flips them GREEN — the demonstration that the
harness catches the seam bug (AC4). Off the per-PR hot path (run on demand, never
collected by pytest), like `news_search_eval`; offline fixture validation
(`tests/agent/evals/test_rag_smoke_yaml.py`) DOES run in the unit sweep.

**Clean-window discipline (Cohere).** Each fixture fires one Cohere rerank call;
the trial tier rate-limits per minute and a DECLINED rerank silently falls back to
the floorless fused path (the QNT-279 floor only applies when the cross-encoder
ran), surfacing sub-floor boilerplate that reads as a guard FAIL on a contaminated
window. `run_all` spaces the fixtures (`--delay`, default 6s) to keep rerank under
the limit; `contamination_warning` flags throttled GRAPH rows (a retrieval-only
row has no generation-latency signal and is never flagged).

**First recorded demonstration** (QNT-278, 2026-06-25, `--retrieval-only`,
12 fixtures, spaced): the harness catches the seam bug exactly as designed.
* **post-QNT-279** (floor on, `main`): **11/11** gated PASS, both earnings
  `boilerplate_guard` rows empty (the floor drops the 8-K "About &lt;co&gt;"
  best-of-weak). `googl-guidance-earnings` returned EMPTY (a recall gap in that
  corpus slice — ungated, not a failure).
* **pre-QNT-279** (floor reverted to 0.0): **5 FAIL** — all four
  `boilerplate_guard` rows surface sub-floor boilerplate as sources (news
  0.036–0.059, earnings 0.076–0.152; all below their 0.30 / 0.50 floors) and the
  exit code flips to 1. That RED→GREEN swing is the proof the harness would have
  caught the QNT-279 leak before prod.

```bash
uv run python -m agent.evals.rag_smoke_eval                   # full chain (needs stack + Cohere)
uv run python -m agent.evals.rag_smoke_eval --retrieval-only  # rerank-floor axis only, no Groq
uv run python -m agent.evals.rag_smoke_eval --only nvda-guidance-earnings
uv run python -m agent.evals.rag_smoke_eval --delay 0         # no spacing (single --only / clean window)
```

## Clean-window re-run for the new ticker universe (QNT-255)

QNT-237 swapped the universe (V/JPM/UNH -> MU/AMD/INTC). Its AC5 eval passed the
HARD gate but the judge QUALITY numbers were degraded by Groq free-tier
rate-limiting mid-session. QNT-255 re-ran the full golden + dialogue suite on a
verified clean window (sha `68facdf`, 2026-06-16) to get trustworthy numbers.

Golden (run `20260616T175146Z-1f38b9`):

| Field | Value |
|---|---:|
| Records | 41 |
| hallucination_ok | 39/41 |
| tool_call_ok | 41/41 |
| provider_failures | 0/41 |
| Composite | 4.29 (F=7.49 S=1.51 C=4.56 A=3.8) |
| Cosine | 0.408 |

Dialogue (run `20260616T182622Z-3bab54-dialogue`):

| Field | Value |
|---|---:|
| Dialogues | 12 |
| Numeric support | 12/12 clean |
| Composite | 0.812 |
| Non-hallucination | 0.983 |
| Analyst-likeness | 0.758 |
| Helpfulness | 0.808 |
| Exploration | 0.662 |
| Voice match | 0.850 |

**No rate-limit degradation, no swap regression.** Golden composite 4.29 is flat
against the last old-universe run (4.33, `20260612`), and the dialogue composite
0.812 sits within one SE of the temp=0 baseline (0.836, `20260606`). The new
tickers clear the hard gate cleanly: MU/AMD/INTC are 8/8 on hallucination and
8/8 on tool-call contracts. The low judge composites on single-fact new-ticker
goldens (e.g. `mu-quickfact-eps`, `mu-fundamental`) are the chronic structure
axis scoring 0 on non-thesis query shapes (set-wide S≈1.5, identical old and new
universe), not a new-ticker gap.

**Note the gap (do not loosen the contract).** The 2 golden hallucination misses
are `tsla-news-sentiment` and `meta-news-sentiment` — both *retained* tickers, so
not swap-induced. A targeted re-run reproduced them (and intermittently
`tsla-news`): the news narrate path emits an unsupported number (TSLA `2.5`, META
`14`) absent from the report strings. This is a pre-existing news-synthesis
prompt-quality issue (see the "news synthesis quoting fundamental-style numbers
is the suspicious one" baseline note above), surfaced clearly on the clean
window. It is the input to a follow-up prompt-tuning ticket; the contract stays
as-is.

## Running locally

Requires the API (`make dev-api`) and LiteLLM (`make dev-litellm`) running, plus an SSH tunnel to ClickHouse (`make tunnel`).

```bash
# All three evals against the full golden set
uv run python -m agent.evals

# Filter to a single ticker for quick iteration
uv run python -m agent.evals --only NVDA

# Write history elsewhere (CI / experimentation)
uv run python -m agent.evals --history-path /tmp/eval-history.csv

# Dialogue-quality evals; opt into Langfuse score emission for manual dev runs
uv run python -m agent.evals.dialogue_eval --history-path /tmp/dialogue-history.csv
uv run python -m agent.evals.dialogue_eval --emit-langfuse-scores

# RAG news-search eval (flag firing + retrieval relevance); needs live Qdrant
uv run python -m agent.evals.news_search_eval
uv run python -m agent.evals.news_search_eval --only nvda-litigation
uv run python -m agent.evals.news_search_eval --flag-only   # skip live Qdrant
```

Exit codes:
- `0` — every record passed the hallucination + tool-call contracts.
- `1` — any record failed a hard contract, OR (if `EVAL_MIN_JUDGE` is set) the average judge score fell below the threshold.

The judge score is treated as a **soft** signal by default — the gate is on hard contracts (hallucination, tool-call). Set `EVAL_MIN_JUDGE=7` once `history.csv` shows enough baseline runs to trust a number.

## Judge axes (QNT-191)

The LLM-as-judge scores four axes independently (each 0–10):

| Axis | What it measures |
|---|---|
| `faithfulness` | Every number in the thesis appears verbatim in the reports. 10 = no fabricated figures. |
| `structure` | All required sections present (Setup, Bull, Bear, Verdict). 10 = fully covered. |
| `correctness` | Conclusions and citations align with the reference thesis. 10 = fully aligned. |
| `analyst_logic` | Four analyst-logic rules respected: |
| | **B-1** Overbought indicators (RSI ≥ 70) must NOT appear as bull-case bullets. |
| | **B-2** SIGNAL-aggregate phrases ("all indicators agree") must NOT appear in a FOCUSED summary. |
| | **B-3** Prior-session delta information must be characterised when present in the report. |
| | **B-8** Verdict-action with a specific level must carry a conditional verb ("if", "should consider"). |

`composite` is the rounded average of all four axes, kept for backwards-compatible trend lines.
Historical rows (before QNT-191) have empty axis columns and carry the old single score in `composite`.

## Reading `history.csv`

Each row is one (run × record) pair. `tool_call_ok` and `hallucination_ok` are the hard contracts (1 = pass, 0 = fail). The committed baseline at QNT-67 ship-time intentionally includes failing rows — the eval surfaced 3 real prompt-quality findings on its first live sweep:

- `amzn-fundamental` — thesis emitted `16.09`, not in any report
- `unh-fundamental` and `unh-news` — thesis emitted `89.02` and `99.82`, not in any report (news synthesis quoting fundamental-style numbers is the suspicious one)

Those are **the framework working as intended**, not framework noise. They're the input to follow-up prompt-tuning tickets, kept in the baseline so the next prompt edit is measurable against this floor.

## Why this lives in the repo, not a separate eval service

- The harness runs the same agent code and prompts that ship to prod — keeping it in-tree makes that automatic.
- CI can run a subset of evals on every PR.
- `history.csv` in git gives a permanent, reviewable record of how prompt changes moved the metrics.
