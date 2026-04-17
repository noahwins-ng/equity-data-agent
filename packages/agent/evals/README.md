# Agent Evaluation Harness (QNT-67)

Evaluation framework for the LangGraph agent. Lives alongside the agent so evals can be run against a locally-running CLI (`python -m agent analyze NVDA`) without starting a server.

**Design intent**: this harness is the single most important piece of AI-Engineering signal in the repo. It operationalises the ADR-003 contract ("the LLM never calculates") and provides a measurable quality signal for the prompt across versions. Reusable enough to extract as a standalone repo later.

## Three eval types — all required, not optional

### (a) Numeric-claim hallucination detector — `hallucination.py`

For every thesis the agent produces, regex every numeric claim out of the text (prices, ratios, percentages, dates). Assert each number appears **verbatim** in one of the tool-output report strings the agent received.

- Any mismatch = test failure.
- This is the direct operational enforcement of ADR-003. If the LLM adds a number that wasn't in a report, it hallucinated — by definition.
- Runs as part of every CI build once the agent is wired up.

### (b) Golden-set regression — `golden_set.py` + `goldens/questions.yaml`

15–20 curated `(ticker, question, reference_thesis, expected_tools)` pairs, one YAML record each.

Per run, for each golden-set record:
1. Invoke the agent CLI with the `(ticker, question)` pair.
2. Score the generated thesis against the reference thesis via:
   - **LLM-as-judge** score (0–10) with a rubric-based prompt.
   - **Cosine similarity** of embedded thesis vs embedded reference (same `all-MiniLM-L6-v2` model already used for news embeddings — no new dependency).
3. Append one row per record to `evals/history.csv`:
   `run_id, git_sha, prompt_version, ticker, question, judge_score, cosine, tool_call_ok, hallucination_ok, elapsed_ms`.
4. Commit `history.csv` so prompt-version quality is visible in `git log -p evals/history.csv`.

This is how prompt regressions are caught *before* they ship. Not optional.

### (c) Tool-call correctness — `tool_calls.py`

For each golden-set question, assert the expected tool was called — e.g.:

- "What's the valuation of X?" MUST call `get_fundamental_report`.
- "How is X trending technically?" MUST call `get_technical_report`.
- "What's the recent news on X?" MUST call `get_news_report` or `search_news`.

Cheap to run, catches prompt regressions that cause the agent to grab the wrong reports.

## Running locally

```bash
# All three evals against the golden set
uv run python -m agent.evals

# Just hallucination check on a single fresh thesis
uv run python -m agent.evals.hallucination --ticker NVDA

# Golden-set regression only (appends to history.csv)
uv run python -m agent.evals.golden_set
```

## Why this lives in the repo, not a separate eval service

- The harness must run against the same agent code and prompts that ship to prod — keeping it in-tree makes that automatic.
- CI can run a subset of evals on every PR.
- `history.csv` committed in-repo gives a permanent, reviewable record of how prompt changes moved the metrics.
