# Fix

Error recovery after a pipeline failure. Diagnoses what went wrong, fixes it, and resumes the `/go` pipeline from the failed step. Pass the issue identifier as an argument (e.g., `/fix QNT-40`).

The issue identifier is: $ARGUMENTS

## Instructions

### Step 1: Diagnose

1. **Check git state** (primary signal — always reliable):
   - `git branch --show-current` — confirm we're on the right branch
   - `git log --oneline main...HEAD` — see commits on branch
   - `git status` — any uncommitted work from the failed run?
   - `gh pr list --head <branch> --state open` — is there an open PR?
   - `gh pr list --head <branch> --state merged` — was the PR already merged?

2. **Determine failure point from git state** (use this hierarchy, not Linear status):
   - **No commits on branch** → `/pick` completed but `/implement` never started
   - **WIP commits only + code quality checks fail** → `/implement` incomplete (lint/type/test failures)
   - **WIP commits only + checks pass + unfinished AC** → `/implement` incomplete (missing AC)
   - **WIP commits only + checks pass + all AC done** → `/sanity-check` or early `/ship` failed
   - **One clean conventional commit (squashed)** → `/ship` failed post-squash (during push, PR, CI, or merge)
   - **Open PR exists** → `/ship` failed during CI or merge step
   - **Merged PR exists** → `/ship` failed during post-deploy verification

3. **Cross-check with Linear** (secondary — may have drifted):
   - Fetch the Linear issue status to confirm, but do NOT override the git-based diagnosis if they disagree
   - If Linear status contradicts git state, note the discrepancy in the report

4. **Report the diagnosis**:
   ```
   Diagnosing: QNT-XX — Title
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━

   Branch:     noahwinsdev/qnt-XX-description
   Linear:     In Progress (matches git state | DRIFTED — git says <X>)
   WIP commits: 3
   Uncommitted: 2 modified files
   Open PR:    none

   Last failure likely at: /implement (Step 2)
   Reason: <inferred from git state — e.g., "lint errors in uncommitted files", "tests failing", "incomplete AC">
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

- If fixed during `/implement` → continue to `/sanity-check` → `/review` → `/ship`
- If fixed during `/sanity-check` → continue to `/review` → `/ship`
- If fixed during `/review` → continue to `/ship`
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
