# Fix

Error recovery after a pipeline failure. Diagnoses what went wrong, fixes it, and resumes the `/go` pipeline from the failed step. Pass the issue identifier as an argument (e.g., `/fix QNT-40`).

The issue identifier is: $ARGUMENTS

## Instructions

### Step 1: Diagnose

1. **Check current state**:
   - `git branch --show-current` — confirm we're on the right branch
   - `git log --oneline main...HEAD` — see WIP commits (tells us how far `/implement` got)
   - `git status` — any uncommitted work from the failed run?
   - Fetch the Linear issue status — tells us which pipeline step was reached:
     - **In Progress** → failed during `/implement` (never reached sanity-check)
     - **In Review** → failed during `/ship` (sanity-check passed but ship failed)
     - **Todo/Backlog** → `/pick` may not have completed

2. **Identify the failure point** from the evidence above and report:
   ```
   Diagnosing: QNT-XX — Title
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━

   Branch:     noahwinsdev/qnt-XX-description
   Linear:     In Progress
   WIP commits: 3
   Uncommitted: 2 modified files

   Last failure likely at: /implement (Step 2)
   Reason: <inferred from state — e.g., "lint errors in uncommitted files", "tests failing", "incomplete AC">
   ```

### Step 2: Fix

Based on the failure point:

**If failed during `/implement`**:
1. Check uncommitted files — stage and commit a WIP if there's salvageable work
2. Run `uv run ruff check .` and `uv run ruff format .` — fix any lint/format issues
3. Run `uv run pyright` — fix any type errors
4. Run `uv run pytest packages/<package>/tests/ -x -q` — fix any test failures
5. Re-read the acceptance criteria from Linear and check which ACs are still unfinished
6. Implement any remaining ACs
7. Create a WIP commit for the fixes

**If failed during `/sanity-check`**:
1. Read the failing check output (lint, format, types, or tests)
2. Fix the specific issues
3. Re-run all checks: `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest`
4. If all pass, move Linear → **In Review**

**If failed during `/ship`**:
1. Check if a PR already exists: look for open PRs on this branch
2. If PR exists but CI failed: read the CI error, fix, push
3. If no PR: resume from the `/ship` step (commit, push, create PR)
4. If merge conflict: report to user — this needs manual resolution

### Step 3: Resume Pipeline

After fixing, resume the `/go` pipeline from where it failed:

- If fixed during `/implement` → continue to `/sanity-check` → `/ship`
- If fixed during `/sanity-check` → continue to `/ship`
- If fixed during `/ship` → complete the ship (CI, merge, cleanup)

### Step 4: Report

```
Fixed: QNT-XX — Title
━━━━━━━━━━━━━━━━━━━━━

Problem:  <what failed>
Fix:      <what was done>
Resumed:  from /sanity-check → /ship

PR:     <url> (merged)
Status: Done
```

If the fix itself fails after 2 attempts, report the specific error and suggest manual intervention.
