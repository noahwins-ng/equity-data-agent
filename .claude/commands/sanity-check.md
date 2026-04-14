# Sanity Check

Pre-PR quality gate. Verifies code quality and acceptance criteria before shipping. Pass the issue identifier as an argument (e.g., `/sanity-check QNT-34`).

The issue identifier is: $ARGUMENTS

## Instructions

### Step 1: Code Quality
Run these checks and report pass/fail for each:
- `uv run ruff check .` (lint)
- `uv run ruff format --check .` (format)
- `uv run pyright` (type check)
- `uv run pytest` (tests)

### Step 2: Acceptance Criteria
1. Fetch the issue from Linear using the provided identifier
2. Extract the **Acceptance Criteria** section from the issue description
3. For each criterion, evaluate whether the current code satisfies it:
   - Read relevant files, run commands, check behavior
   - Mark each as: PASS / FAIL / NEEDS MANUAL VERIFICATION

### Step 3: Report
Format the results as:

```
Sanity Check: QNT-XX — Title
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Code Quality:
  ✓ Lint          passed
  ✓ Format        passed
  ✓ Type Check    passed
  ✓ Tests         passed (X tests)

Acceptance Criteria:
  ✓ Criterion 1 description
  ✓ Criterion 2 description
  ✗ Criterion 3 description — [reason]

Verdict: READY TO SHIP / NEEDS FIXES
```

### Step 4: If NEEDS FIXES
List the specific issues found and offer to fix them before shipping.
