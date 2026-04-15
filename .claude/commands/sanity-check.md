# Sanity Check

Pre-PR quality gate. Verifies code quality and acceptance criteria before shipping. Pass the issue identifier as an argument (e.g., `/sanity-check QNT-34`).

The issue identifier is: $ARGUMENTS

## Instructions

### Step 0: Check for Uncommitted Work
Run `git status`. If there are uncommitted changes or untracked files:
- Warn the user: "Uncommitted work detected — checks will run on the committed state. Stage and commit first if you want these included."
- Proceed with checks anyway, but flag this in the report.

### Step 1: Code Quality
Run these checks and report pass/fail for each:
- `uv run ruff check .` (lint)
- `uv run ruff format --check .` (format)
- `uv run pyright` (type check)
- `uv run pytest` (tests)

### Step 2: Acceptance Criteria
1. Fetch the issue from Linear using the provided identifier
2. Extract the **Acceptance Criteria** section from the issue description
3. For each criterion, **classify it before evaluating**:
   - **[code AC]** — verifiable by reading the implementation (e.g., "handles 429 with exponential backoff", "uses ReplacingMergeTree", "validates ticker against TICKERS"). Read relevant files and mark PASS / FAIL.
   - **[dev execution AC]** — must actually run locally before ship (e.g., "backfill ran successfully", "no duplicates on re-run", "asset visible in Dagster lineage graph", "endpoint returns 200"). **You must run the verification command and paste its output as evidence.** Classification alone is not enough — this has failed twice (QNT-41, QNT-42).
     - **Keyword trigger**: if an AC contains "populated", "data in", "visible", "returns", "responds", "runs", "backfill", "no duplicates", "row count", "available", "accessible", or "healthy" — it is ALWAYS a dev/prod execution AC, never a code AC.
     - **Evidence format** (required for every dev execution AC):
       ```
       ✓ AC text  [dev execution AC]
         Command: <exact command you ran>
         Output:  <actual output>
       ```
     - If you cannot show command + output, mark `✗ BLOCKED` and tell the user exactly what to run. **Blocks ship.**
     - Note: for data assets, running locally with `make tunnel` active writes to the same Hetzner ClickHouse as prod — tunnel-verified data counts as prod data.
   - **[prod execution AC]** — can only be confirmed in the deployed environment after merge (e.g., "prod service healthy after deploy", "prod Dagster can trigger the asset", "prod API endpoint responds correctly"). Mark `⏳ PENDING — verify post-deploy`. **Does not block ship, but blocks Linear → Done.**
4. Any `✗ BLOCKED` dev execution AC means NEEDS FIXES — do not proceed to ship until resolved.
5. Any `⏳ PENDING` prod execution AC is carried forward into the `/ship` post-deploy verification step.

### Step 3: Report
Format the results as:

```
Sanity Check: QNT-XX — Title
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Warnings:
  ⚠ Uncommitted work detected — checks ran on committed state  (if applicable)

Code Quality:
  ✓ Lint          passed
  ✓ Format        passed
  ✓ Type Check    passed
  ✓ Tests         passed (X tests)

Acceptance Criteria:
  ✓ Criterion 1  [code AC — verified in src/foo.py:42]
  ✓ Criterion 2  [dev execution AC]
    Command: ssh hetzner "docker exec clickhouse clickhouse-client --query 'SELECT count() FROM ...'"
    Output:  500
  ✗ Criterion 3 — BLOCKED  [dev execution AC — run: make tunnel && make dev-dagster, then re-run asset]
  ⏳ Criterion 4 — PENDING  [prod execution AC — verify post-deploy: make check-prod]

Verdict: READY TO SHIP / NEEDS FIXES
  (READY TO SHIP = all code AC ✓ + all dev execution AC ✓, prod AC pending is acceptable)
```

### Step 4: If READY TO SHIP
Move the Linear issue status to **In Review**.

Post a comment on the Linear issue:
```
**Sanity check passed** — ready for review

✓ Lint  ✓ Format  ✓ Types  ✓ Tests (X passed)  ✓ AC (dev)

Prod execution AC pending post-deploy:
- <list each ⏳ PENDING item, or "none" if all AC were code/dev>
```

### Step 5: If NEEDS FIXES
List the specific issues found and offer to fix them before shipping.
