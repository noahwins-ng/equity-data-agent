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
