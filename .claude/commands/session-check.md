# Session Check

Restore context for the current work-in-progress. Run this at the start of a new Claude Code session when you're picking up where you left off.

## Instructions

1. **Detect current branch** via `git branch --show-current`
   - Extract the QNT issue number from the branch name (e.g., `noahwinsdev/qnt-41-dagster-asset` → QNT-41)
   - If on `main`, skip to step 5

2. **Fetch issue context** from Linear using the extracted issue ID:
   - Title, description, acceptance criteria
   - Current status (In Progress, In Review, etc.)
   - Which milestone it belongs to
   - Identify which system area the issue touches (Dagster, API, agent, shared, frontend, infra) from the title/description
   - Run `git log --oneline main...HEAD` and check if any commits touch the relevant package path (e.g., `packages/dagster-pipelines/` for Dagster issues). If no commits exist for that area on this branch yet, suggest: "No prior commits in this area — read `docs/architecture/system-overview.md` to orient before coding."

3. **Show recent work** to restore context:
   - `git log --oneline main...HEAD` — commits on this branch only
   - `git diff --stat main...HEAD` — files changed vs main
   - `git status` — any uncommitted work

4. **Assess AC status** — for each acceptance criterion:
   - Identify the 1-2 most relevant source files based on the system area from Step 2
   - Read those files and check whether the implementation exists
   - Mark ✓ if clearly present, ○ if partial or uncertain, ✗ if not found
   - Keep this fast and directional — deep verification is `/sanity-check`'s job

5. **If on main** (no active issue):
   - Check for any open PRs via `gh pr list --author @me`
   - Report any open PRs
   - Do NOT suggest the next issue — that's `/cycle-start`'s job
   - Prompt: "No active issue. Run `/cycle-start` to review the cycle or `/pick QNT-XX` to start an issue."

6. **Report** formatted as:
   ```
   Resuming: QNT-XX — Title
   ━━━━━━━━━━━━━━━━━━━━━━━━━
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
