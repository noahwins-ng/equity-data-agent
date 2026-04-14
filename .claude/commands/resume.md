# Resume

Restore context for the current work-in-progress. Run this at the start of a new session when you're picking up where you left off.

## Instructions

1. **Detect current branch** via `git branch --show-current`
   - Extract the QNT issue number from the branch name (e.g., `noahwinsdev/qnt-41-dagster-asset` → QNT-41)
   - If on `main`, skip to step 4

2. **Fetch issue context** from Linear using the extracted issue ID:
   - Title, description, acceptance criteria
   - Current status (In Progress, In Review, etc.)
   - Which milestone it belongs to

3. **Show recent work** to restore context:
   - `git log --oneline -10` — recent commits on this branch
   - `git diff --stat main...HEAD` — files changed vs main
   - `git status` — any uncommitted work

4. **If on main** (no active issue):
   - Check for any open PRs via `gh pr list --author @me`
   - Fetch the active cycle from Linear and show pending issues
   - Suggest the next issue to pick up

5. **Report** formatted as:
   ```
   Resuming: QNT-XX — Title
   ━━━━━���━━━━━━━��━━━━━━━━━━
   Status:    In Progress
   Milestone: Phase X — Name
   Branch:    noahwinsdev/qnt-XX-description

   Recent commits:
     abc1234 QNT-XX: feat(scope): latest change
     def5678 QNT-XX: feat(scope): earlier change

   Files changed (vs main):
     packages/shared/src/shared/config.py
     packages/dagster-pipelines/src/...

   Uncommitted: 2 modified, 1 untracked

   Acceptance Criteria:
     ✓ Criterion 1 (done — committed in abc1234)
     ○ Criterion 2 (in progress)
     ○ Criterion 3 (not started)
   ```
