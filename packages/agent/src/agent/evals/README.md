# Agent Evaluation Harness (QNT-67)

Evaluation framework for the LangGraph agent. Lives in-tree under `packages/agent/src/agent/evals/` so evals run against the exact code that ships, against a locally-running CLI (`uv run python -m agent analyze NVDA`).

**Design intent**: this harness is the single most important piece of AI-Engineering signal in the repo. It operationalises the ADR-003 contract ("the LLM never calculates") and provides a measurable quality signal for the prompt across versions. Reusable enough to extract as a standalone repo later.

## Three eval types — all required, not optional

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
   - **LLM-as-judge** score (0–10) using the agent's own LLM via the LiteLLM proxy at `temperature=0.0`.
   - **Cosine similarity** over normalised term-frequency vectors. The original spec called for `all-MiniLM-L6-v2` embeddings; we ship the lighter zero-dep equivalent (same operation in a different vector space) to keep the harness portable. Swap `similarity.cosine` for an embedding-backed implementation behind the same signature when MiniLM is on the path.
3. Append one row per record to `history.csv`:
   `run_id, git_sha, prompt_version, ticker, question_id, question, judge_score, cosine, tool_call_ok, hallucination_ok, elapsed_ms`.
4. `history.csv` is committed so prompt-version quality is visible in `git log -p packages/agent/src/agent/evals/history.csv`.

### (c) Tool-call correctness — `tool_calls.py`

For each record, assert every tool in `expected_tools` was actually called. Over-fetching is allowed (the planner is told to over-fetch when in doubt) — only under-fetching fails.

## Running locally

Requires the API (`make dev-api`) and LiteLLM (`make dev-litellm`) running, plus an SSH tunnel to ClickHouse (`make tunnel`).

```bash
# All three evals against the full golden set
uv run python -m agent.evals

# Filter to a single ticker for quick iteration
uv run python -m agent.evals --only NVDA

# Write history elsewhere (CI / experimentation)
uv run python -m agent.evals --history-path /tmp/eval-history.csv
```

Exit codes:
- `0` — every record passed the hallucination + tool-call contracts.
- `1` — any record failed a hard contract, OR (if `EVAL_MIN_JUDGE` is set) the average judge score fell below the threshold.

The judge score is treated as a **soft** signal by default — the gate is on hard contracts (hallucination, tool-call). Set `EVAL_MIN_JUDGE=7` once `history.csv` shows enough baseline runs to trust a number.

## Reading `history.csv`

Each row is one (run × record) pair. `tool_call_ok` and `hallucination_ok` are the hard contracts (1 = pass, 0 = fail). The committed baseline at QNT-67 ship-time intentionally includes failing rows — the eval surfaced 3 real prompt-quality findings on its first live sweep:

- `amzn-fundamental` — thesis emitted `16.09`, not in any report
- `unh-fundamental` and `unh-news` — thesis emitted `89.02` and `99.82`, not in any report (news synthesis quoting fundamental-style numbers is the suspicious one)

Those are **the framework working as intended**, not framework noise. They're the input to follow-up prompt-tuning tickets, kept in the baseline so the next prompt edit is measurable against this floor.

## Why this lives in the repo, not a separate eval service

- The harness runs the same agent code and prompts that ship to prod — keeping it in-tree makes that automatic.
- CI can run a subset of evals on every PR.
- `history.csv` in git gives a permanent, reviewable record of how prompt changes moved the metrics.
